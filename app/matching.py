"""Rule-based matching between job seekers and jobs.

Two scores, both 0-100 with plain-language reasons:
- match_score(profile, job): how well a job fits a seeker (recommendations and alerts).
- applicant_score(...): how well an applicant fits a job (the employer's pipeline).

The rules are deliberately simple and explainable. An AI layer can re-rank on top of these later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import CandidateProfile, Job, JobQuestion, split_pipes

LEVEL_ORDER = {"Entry level": 0, "Mid level": 1, "Senior": 2}


@dataclass
class Score:
    score: int
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"score": self.score, "reasons": self.reasons, "flags": self.flags}


def _has(text: str, skill: str) -> bool:
    s = skill.lower().strip()
    if not s:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", text) is not None


def _years_level(years: int | None) -> str | None:
    if years is None:
        return None
    return "Entry level" if years < 2 else ("Mid level" if years < 5 else "Senior")


def _peso(n: int) -> str:
    return f"₱{n:,}"


def job_text(job: Job) -> str:
    return " ".join([job.title or "", job.category or "", job.department or "", " ".join(job.skill_list),
                     job.description_text or ""]).lower()


def match_score(profile: CandidateProfile, job: Job) -> Score:
    reasons: list[str] = []
    pts = 0.0
    skills = profile.skill_list
    text = job_text(job)

    # Skills: 45
    if job.skill_list:
        wanted = job.skill_list
        hit = [s for s in wanted if any(s.lower() == p.lower() for p in skills)]
        pts += 45 * len(hit) / len(wanted)
        if hit:
            reasons.append(f"You have {len(hit)} of {len(wanted)} skills listed: {', '.join(hit[:4])}")
    elif skills:
        hit = [s for s in skills if _has(text, s)]
        pts += 45 * min(1.0, len(hit) / 3)
        if hit:
            reasons.append(f"Mentions your skills: {', '.join(hit[:4])}")

    # Category: 15
    cats = split_pipes(profile.desired_categories)
    if not cats:
        pts += 7
    elif job.category in cats:
        pts += 15
        reasons.append(f"In {job.category}, a field you chose")

    # Setup and location: 15
    setups = split_pipes(profile.desired_setups)
    if job.setup == "Remote" and (not setups or "Remote" in setups):
        pts += 15
        if setups:
            reasons.append("Remote, as you prefer")
    elif profile.location and job.location == profile.location and (not setups or job.setup in setups):
        pts += 15
        reasons.append(f"In {job.location}, near you")
    elif not setups and not profile.location:
        pts += 7
    elif job.setup in setups:
        pts += 8

    # Salary: 10
    if profile.desired_salary and job.salary_max:
        if job.salary_max >= profile.desired_salary:
            pts += 10
            reasons.append(f"Pays up to {_peso(job.salary_max)}, at or above your {_peso(profile.desired_salary)} target")
    else:
        pts += 5

    # Experience: 10
    lvl = _years_level(profile.years_experience)
    if lvl is None:
        pts += 5
    else:
        gap = abs(LEVEL_ORDER.get(lvl, 1) - LEVEL_ORDER.get(job.level, 1))
        pts += 10 if gap == 0 else (5 if gap == 1 else 0)
        if gap == 0:
            reasons.append(f"{job.level} fits your experience")

    # Job type: 5
    types = split_pipes(profile.desired_job_types)
    if not types:
        pts += 3
    elif job.job_type in types:
        pts += 5

    return Score(round(min(100, pts)), reasons)


def check_answers(questions: list[JobQuestion], answers: dict[int, str]) -> list[str]:
    """Names of knockout questions the applicant didn't pass."""
    failed = []
    for q in questions:
        if q.knockout_answer and q.kind in ("yesno", "choice"):
            if (answers.get(q.id) or "").strip().lower() != q.knockout_answer.strip().lower():
                failed.append(q.text)
    return failed


def applicant_score(job: Job, profile_skills: list[str], years: int | None, desired_salary: int | None,
                    resume_text: str, has_resume: bool, failed_knockouts: list[str]) -> Score:
    reasons: list[str] = []
    flags: list[str] = []
    pts = 0.0
    corpus = (" ".join(profile_skills) + " " + (resume_text or "")).lower()

    # Skills: 60
    wanted = job.skill_list
    if wanted:
        hit = [s for s in wanted if _has(corpus, s)]
        pts += 60 * len(hit) / len(wanted)
        reasons.append(f"Has {len(hit)} of {len(wanted)} required skills" + (f": {', '.join(hit[:5])}" if hit else ""))
        missing = [s for s in wanted if s not in hit]
        if missing:
            flags.append(f"Missing: {', '.join(missing[:5])}")
    else:
        hit = [s for s in profile_skills if _has(job_text(job), s)]
        pts += 60 * min(1.0, len(hit) / 3)
        if hit:
            reasons.append(f"Skills mentioned in the job: {', '.join(hit[:5])}")

    # Experience: 20
    lvl = _years_level(years)
    if lvl is not None:
        gap = abs(LEVEL_ORDER.get(lvl, 1) - LEVEL_ORDER.get(job.level, 1))
        pts += 20 if gap == 0 else (10 if gap == 1 else 0)
        reasons.append(f"{years} year{'s' if years != 1 else ''} of experience")
    else:
        pts += 8

    # Salary expectation: 10
    if desired_salary and job.salary_max:
        if desired_salary <= job.salary_max:
            pts += 10
        else:
            flags.append(f"Expects {_peso(desired_salary)}, above your range")
    else:
        pts += 5

    # Resume attached: 10
    if has_resume:
        pts += 10
    else:
        flags.append("No resume attached")

    score = round(min(100, pts))
    if failed_knockouts:
        score = min(score, 20)
        flags.insert(0, "Didn't meet: " + "; ".join(failed_knockouts))
    return Score(score, reasons, flags)
