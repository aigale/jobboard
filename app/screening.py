"""Screening tools: timed skill tests, the follow-up screening chat, and the optional AI fit summary."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from . import ai
from .auth import require_employer, require_seeker
from .config import settings
from .db import get_session
from .employer import _own_application, need_company
from .mailer import flush_soon, queue_email
from .models import (Application, ApplicationEvent, Assessment, AssessmentAttempt, Company, Document, Job,
                     Notification, ScreeningChat, User, aware, utcnow)

employer_router = APIRouter(prefix="/api/employer", tags=["screening (employers)"])
seeker_router = APIRouter(prefix="/api/me", tags=["screening (job seekers)"])
GRACE = timedelta(seconds=30)          # network delay allowance when submitting a timed test
MAX_CHAT_TURNS = 12


# ---------------------------------------------------------------------------
# Assessments: employer side
# ---------------------------------------------------------------------------

class TestQuestionIn(BaseModel):
    text: str = Field(min_length=3, max_length=1000)
    options: list[str] = Field(min_length=2, max_length=6)
    answer: int = Field(ge=0)

    @model_validator(mode="after")
    def _check(self):
        self.options = [o.strip()[:300] for o in self.options]
        if any(not o for o in self.options):
            raise ValueError(f"Every choice needs text in: {self.text[:60]}")
        if self.answer >= len(self.options):
            raise ValueError(f"Pick the correct choice for: {self.text[:60]}")
        return self


class AssessmentIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    instructions: str = Field("", max_length=3000)
    time_limit_minutes: int = Field(15, ge=1, le=180)
    pass_score: int = Field(60, ge=0, le=100)
    questions: list[TestQuestionIn] = Field(min_length=1, max_length=50)


def assessment_out(a: Assessment, db: Session | None = None) -> dict:
    out = {"id": a.id, "title": a.title, "instructions": a.instructions, "time_limit_minutes": a.time_limit_minutes,
           "pass_score": a.pass_score, "questions": a.questions, "question_count": len(a.questions or [])}
    if db is not None:
        out["jobs"] = [{"id": j.id, "title": j.title} for j in db.scalars(select(Job).where(Job.assessment_id == a.id))]
        out["taken"] = db.scalar(select(func.count()).select_from(AssessmentAttempt).where(
            AssessmentAttempt.assessment_id == a.id, AssessmentAttempt.submitted_at.is_not(None)))
    return out


def _own_assessment(db: Session, company: Company, aid: int) -> Assessment:
    a = db.get(Assessment, aid)
    if a is None or a.company_id != company.id:
        raise HTTPException(404, "Test not found.")
    return a


@employer_router.get("/assessments")
def list_assessments(company: Company = Depends(need_company), db: Session = Depends(get_session)):
    return [assessment_out(a, db) for a in db.scalars(select(Assessment).where(Assessment.company_id == company.id)
                                                      .order_by(Assessment.id.desc()))]


@employer_router.post("/assessments", status_code=201)
def create_assessment(body: AssessmentIn, company: Company = Depends(need_company), db: Session = Depends(get_session)):
    a = Assessment(company_id=company.id, **body.model_dump(exclude={"questions"}),
                   questions=[q.model_dump() for q in body.questions])
    db.add(a)
    db.commit()
    return assessment_out(a, db)


@employer_router.put("/assessments/{aid}")
def update_assessment(aid: int, body: AssessmentIn, company: Company = Depends(need_company),
                      db: Session = Depends(get_session)):
    a = _own_assessment(db, company, aid)
    started = db.scalar(select(func.count()).select_from(AssessmentAttempt).where(
        AssessmentAttempt.assessment_id == a.id, AssessmentAttempt.started_at.is_not(None)))
    if started and [q.model_dump() for q in body.questions] != a.questions:
        raise HTTPException(400, "Candidates have already taken this test, so its questions can't change. "
                                 "Make a copy instead so everyone is scored on the same questions.")
    for k, v in body.model_dump(exclude={"questions"}).items():
        setattr(a, k, v)
    a.questions = [q.model_dump() for q in body.questions]
    db.commit()
    return assessment_out(a, db)


@employer_router.delete("/assessments/{aid}")
def delete_assessment(aid: int, company: Company = Depends(need_company), db: Session = Depends(get_session)):
    a = _own_assessment(db, company, aid)
    if db.scalar(select(func.count()).select_from(AssessmentAttempt).where(AssessmentAttempt.assessment_id == a.id)):
        raise HTTPException(400, "This test was already sent to candidates. Keep it so their results stay visible.")
    for j in db.scalars(select(Job).where(Job.assessment_id == a.id)):
        j.assessment_id, j.assessment_auto = None, False
    db.delete(a)
    db.commit()
    return {"ok": True}


def send_test(db: Session, application: Application, assessment: Assessment, actor_id: int | None) -> AssessmentAttempt:
    existing = db.scalar(select(AssessmentAttempt).where(AssessmentAttempt.application_id == application.id,
                                                         AssessmentAttempt.assessment_id == assessment.id))
    if existing:
        return existing
    att = AssessmentAttempt(application_id=application.id, assessment_id=assessment.id)
    db.add(att)
    db.flush()
    db.add(ApplicationEvent(application_id=application.id, kind="assessment", note=f"Test sent: {assessment.title}",
                            actor_id=actor_id, visible_to_candidate=True))
    company = db.get(Company, assessment.company_id)
    db.add(Notification(user_id=application.user_id, text=f"{company.name} sent you a skills test: {assessment.title}",
                        link=f"/test.html?id={att.id}"))
    user = db.get(User, application.user_id)
    queue_email(db, user.email, f"Skills test from {company.name}",
                f"Hi {user.name},\n\n{company.name} would like you to take a short test for your application: "
                f"{assessment.title} ({len(assessment.questions)} questions, {assessment.time_limit_minutes} minutes).\n\n"
                f"The timer starts only when you press Start:\n{settings.public_url}/test.html?id={att.id}\n")
    return att


class SendTestIn(BaseModel):
    assessment_id: int


@employer_router.post("/applications/{app_id}/assessment", status_code=201)
def send_test_to_applicant(app_id: int, body: SendTestIn, background: BackgroundTasks,
                           user: User = Depends(require_employer), company: Company = Depends(need_company),
                           db: Session = Depends(get_session)):
    a = _own_application(db, company, app_id)
    if a.stage in ("withdrawn", "rejected", "hired"):
        raise HTTPException(400, "This application is closed.")
    test = _own_assessment(db, company, body.assessment_id)
    att = send_test(db, a, test, user.id)
    db.commit()
    flush_soon(background)
    return {"ok": True, "attempt_id": att.id, "message": f"Test sent to {(a.profile_snapshot or {}).get('full_name', 'the candidate')}."}


def attempt_summary(att: AssessmentAttempt) -> dict:
    return {"id": att.id, "title": att.assessment.title, "status": attempt_status(att), "score": att.score,
            "passed": att.passed, "late": att.late, "sent_at": aware(att.sent_at), "submitted_at": aware(att.submitted_at),
            "pass_score": att.assessment.pass_score}


def attempt_status(att: AssessmentAttempt) -> str:
    if att.submitted_at:
        return "Completed"
    if att.started_at:
        return "Time's up" if utcnow() > aware(att.deadline) + GRACE else "In progress"
    return "Not started"


def attempts_for(db: Session, application_id: int) -> list[AssessmentAttempt]:
    return db.scalars(select(AssessmentAttempt).options(joinedload(AssessmentAttempt.assessment))
                      .where(AssessmentAttempt.application_id == application_id).order_by(AssessmentAttempt.id)).all()


def employer_attempt_detail(att: AssessmentAttempt) -> dict:
    out = attempt_summary(att)
    if att.submitted_at:
        out["review"] = [{"text": q["text"], "options": q["options"], "correct": q["answer"],
                          "chosen": (att.answers[i] if i < len(att.answers) else None)}
                         for i, q in enumerate(att.assessment.questions)]
    return out


# ---------------------------------------------------------------------------
# Assessments: job seeker side
# ---------------------------------------------------------------------------

def _own_attempt(db: Session, user: User, attempt_id: int) -> AssessmentAttempt:
    att = db.scalar(select(AssessmentAttempt).options(joinedload(AssessmentAttempt.assessment),
                                                      joinedload(AssessmentAttempt.application))
                    .where(AssessmentAttempt.id == attempt_id))
    if att is None or att.application.user_id != user.id:
        raise HTTPException(404, "Test not found.")
    return att


def _seeker_view(db: Session, att: AssessmentAttempt) -> dict:
    job = db.get(Job, att.application.job_id)
    company = db.get(Company, job.company_id)
    t = att.assessment
    out = {**attempt_summary(att), "instructions": t.instructions, "time_limit_minutes": t.time_limit_minutes,
           "question_count": len(t.questions), "job": job.title, "company": company.name,
           "deadline": aware(att.deadline), "seconds_left": None, "questions": None}
    if att.started_at and not att.submitted_at:
        out["seconds_left"] = max(0, int((aware(att.deadline) - utcnow()).total_seconds()))
        out["questions"] = [{"text": q["text"], "options": q["options"]} for q in t.questions]   # never the answers
        out["answers"] = att.answers
    return out


@seeker_router.get("/tests")
def my_tests(user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    rows = db.scalars(select(AssessmentAttempt).options(joinedload(AssessmentAttempt.assessment),
                                                        joinedload(AssessmentAttempt.application))
                      .join(Application).where(Application.user_id == user.id)
                      .order_by(AssessmentAttempt.id.desc())).all()
    return [_seeker_view(db, a) for a in rows]


@seeker_router.get("/tests/{attempt_id}")
def get_test(attempt_id: int, user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    return _seeker_view(db, _own_attempt(db, user, attempt_id))


@seeker_router.post("/tests/{attempt_id}/start")
def start_test(attempt_id: int, user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    att = _own_attempt(db, user, attempt_id)
    if att.application.stage in ("withdrawn", "rejected"):
        raise HTTPException(400, "This application is closed, so the test can't be taken.")
    if att.started_at is None:
        att.started_at = utcnow()
        att.deadline = att.started_at + timedelta(minutes=att.assessment.time_limit_minutes)
        att.answers = [None] * len(att.assessment.questions)
        db.commit()
    return _seeker_view(db, att)


class AnswersIn(BaseModel):
    answers: list[int | None] = Field(max_length=50)
    final: bool = True       # False = save progress only


@seeker_router.post("/tests/{attempt_id}/answers")
def submit_test(attempt_id: int, body: AnswersIn, background: BackgroundTasks,
                user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    att = _own_attempt(db, user, attempt_id)
    if att.started_at is None:
        raise HTTPException(400, "Press Start first.")
    if att.submitted_at is not None:
        raise HTTPException(400, "You already submitted this test.")
    qs = att.assessment.questions
    answers = (list(body.answers) + [None] * len(qs))[:len(qs)]
    answers = [a if (a is not None and 0 <= a < len(qs[i]["options"])) else None for i, a in enumerate(answers)]
    now = utcnow()
    late = now > aware(att.deadline) + GRACE
    if not body.final and not late:
        att.answers = answers
        db.commit()
        return _seeker_view(db, att)
    if late:
        # Past the time limit: only answers saved before the deadline count.
        answers = list(att.answers or [None] * len(qs))
    att.answers = answers
    att.submitted_at, att.late = now, late
    correct = sum(1 for i, q in enumerate(qs) if answers[i] == q["answer"])
    att.score = round(100 * correct / len(qs))
    att.passed = att.score >= att.assessment.pass_score
    db.add(ApplicationEvent(application_id=att.application_id, kind="assessment", actor_id=user.id,
                            note=f"Test completed: {att.assessment.title} ({att.score}%)", visible_to_candidate=True))
    job = db.get(Job, att.application.job_id)
    from .models import CompanyMember
    for m in db.scalars(select(CompanyMember).where(CompanyMember.company_id == job.company_id)):
        db.add(Notification(user_id=m.user_id, text=f"{user.name} finished the {att.assessment.title} test: {att.score}%",
                            link=f"/employer.html#pipeline-{job.id}"))
    db.commit()
    flush_soon(background)
    return {**_seeker_view(db, att), "message": "Test submitted. The employer will see your result."}


# ---------------------------------------------------------------------------
# Screening chat
# ---------------------------------------------------------------------------

def _bot(text: str) -> dict:
    return {"role": "bot", "text": text, "at": utcnow().isoformat()}


def start_chat(db: Session, application: Application, job: Job, company: Company) -> ScreeningChat | None:
    topics = [t for t in (job.chat_topics or []) if t.strip()]
    if not job.chat_screening or not topics:
        return None
    chat = ScreeningChat(application_id=application.id, mode="ai" if ai.enabled() else "scripted")
    first = (f"Hi! Thanks for applying to {job.title} at {company.name}. I have {len(topics)} quick follow-up "
             f"question{'s' if len(topics) != 1 else ''} for you. It takes about 2 minutes.")
    chat.messages = [_bot(first), _bot(_scripted_question(topics[0]))]
    db.add(chat)
    db.add(Notification(user_id=application.user_id, text=f"{company.name} has a few follow-up questions about your application",
                        link=f"/chat.html?app={application.id}"))
    return chat


def _scripted_question(topic: str) -> str:
    t = topic.strip()
    return t if t.endswith("?") else f"Could you tell us about your {t[0].lower() + t[1:]}?"


def _scripted_summary(chat: ScreeningChat) -> str:
    lines, q = [], None
    for m in chat.messages[1:]:
        if m["role"] == "bot":
            q = m["text"]
        elif q:
            lines.append(f"Q: {q}\nA: {m['text']}")
            q = None
    return "\n\n".join(lines)


def _ai_turn(chat: ScreeningChat, job: Job, company: Company) -> tuple[str, bool]:
    topics = "\n".join(f"- {t}" for t in job.chat_topics or [])
    system = (f"You are a friendly screening assistant for {company.name}, hiring a {job.title} in the Philippines. "
              f"Ask the candidate about each of these topics, one short question at a time, in the order given:\n{topics}\n"
              "Follow up once only if an answer is unclear. Keep every message under 40 words. Reply in the language "
              "the candidate uses (English, Filipino or Taglish). When every topic is covered, thank them, say the "
              "team will review their application, and end your message with [DONE]. " + ai.FAIRNESS)
    msgs = []
    for m in chat.messages:
        role = "assistant" if m["role"] == "bot" else "user"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n\n" + m["text"]
        else:
            msgs.append({"role": role, "content": m["text"]})
    if msgs and msgs[0]["role"] == "assistant":
        msgs.insert(0, {"role": "user", "content": "(The candidate opened the chat.)"})
    reply = ai.complete(system, msgs, max_tokens=200)
    done = "[DONE]" in reply
    return reply.replace("[DONE]", "").strip(), done


def _ai_summary(chat: ScreeningChat, job: Job) -> str:
    transcript = "\n".join(f"{'Assistant' if m['role'] == 'bot' else 'Candidate'}: {m['text']}" for m in chat.messages)
    system = ("Summarize this screening chat for a recruiter as short bullet points: one bullet per topic with the "
              "candidate's answer in their own words, plus anything they said they need. No opinions or scores. "
              + ai.FAIRNESS)
    return ai.complete(system, [{"role": "user", "content": f"Job: {job.title}\nTopics: {', '.join(job.chat_topics or [])}\n\n{transcript}"}],
                       max_tokens=300)


def chat_out(chat: ScreeningChat) -> dict:
    return {"id": chat.id, "status": chat.status, "mode": chat.mode, "messages": chat.messages,
            "summary": chat.summary, "finished_at": aware(chat.finished_at)}


def _own_chat(db: Session, user: User, app_id: int) -> tuple[ScreeningChat, Application]:
    a = db.get(Application, app_id)
    if a is None or a.user_id != user.id:
        raise HTTPException(404, "Application not found.")
    chat = db.scalar(select(ScreeningChat).where(ScreeningChat.application_id == a.id))
    if chat is None:
        raise HTTPException(404, "This application has no follow-up questions.")
    return chat, a


@seeker_router.get("/applications/{app_id}/chat")
def get_chat(app_id: int, user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    chat, a = _own_chat(db, user, app_id)
    job = db.get(Job, a.job_id)
    return {**chat_out(chat), "summary": None, "job": job.title, "company": db.get(Company, job.company_id).name}


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


@seeker_router.post("/applications/{app_id}/chat")
def chat_reply(app_id: int, body: ChatIn, background: BackgroundTasks, user: User = Depends(require_seeker),
               db: Session = Depends(get_session)):
    chat, a = _own_chat(db, user, app_id)
    if chat.status == "done":
        raise HTTPException(400, "This chat is finished. Thanks for your answers!")
    if a.stage in ("withdrawn", "rejected"):
        raise HTTPException(400, "This application is closed.")
    job = db.get(Job, a.job_id)
    company = db.get(Company, job.company_id)
    topics = [t for t in (job.chat_topics or []) if t.strip()] or ["Anything else you'd like us to know"]
    msgs = list(chat.messages) + [{"role": "candidate", "text": body.text.strip(), "at": utcnow().isoformat()}]
    chat.messages = msgs
    turns = sum(1 for m in msgs if m["role"] == "candidate")
    done = False
    if chat.mode == "ai":
        try:
            reply, done = _ai_turn(chat, job, company)
        except ai.AIError:
            chat.mode = "scripted"             # keep going without AI rather than failing the candidate
            chat.topic_index = min(turns - 1, len(topics) - 1)
            reply = None
        if reply is not None:
            done = done or turns >= MAX_CHAT_TURNS
            chat.messages = msgs + [_bot(reply)]
    if chat.mode == "scripted":
        chat.topic_index += 1
        if chat.topic_index < len(topics):
            chat.messages = msgs + [_bot(_scripted_question(topics[chat.topic_index]))]
        else:
            done = True
            chat.messages = msgs + [_bot(f"Thank you! That's everything. The {company.name} team will review your answers with your application.")]
    if done:
        chat.status, chat.finished_at = "done", utcnow()
        summary = None
        if chat.mode == "ai":
            try:
                summary = _ai_summary(chat, job)
            except ai.AIError:
                summary = None
        chat.summary = summary or _scripted_summary(chat)
        db.add(ApplicationEvent(application_id=a.id, kind="chat", note="Answered the follow-up questions",
                                actor_id=user.id, visible_to_candidate=True))
    db.commit()
    return {**chat_out(chat), "summary": None}


# ---------------------------------------------------------------------------
# AI fit summary for employers
# ---------------------------------------------------------------------------

@employer_router.post("/applications/{app_id}/ai-summary")
def ai_fit(app_id: int, refresh: bool = False, company: Company = Depends(need_company), db: Session = Depends(get_session)):
    a = _own_application(db, company, app_id)
    if a.ai_summary and not refresh:
        return {"summary": a.ai_summary}
    job = a.job
    resumes = db.scalars(select(Document).where(Document.id.in_(a.document_ids or [-1]), Document.kind == "resume")).all()
    try:
        a.ai_summary = ai.fit_summary(job.title, company.name, job.description_text or "", job.skill_list,
                                      a.profile_snapshot or {}, "\n".join(d.text or "" for d in resumes), a.answers)
    except ai.AIError as exc:
        raise HTTPException(400, str(exc))
    db.commit()
    return {"summary": a.ai_summary}
