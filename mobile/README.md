# Sinag Jobs phone app (Android and iPhone)

This folder is a [Capacitor](https://capacitorjs.com) project. The app opens your live Sinag Jobs site
in a native shell and adds what a website can't do well on phones:

- **Native push notifications** through Firebase Cloud Messaging (Android and iPhone).
- **Social sign-in that works in an app.** Google and Facebook block sign-in inside apps, so the app opens
  the phone's browser and comes back with a one-time code that only this app can use.
- **Android back button**, an **offline screen** (English, Tagalog, Cebuano), app icon and splash screen.

Everything else (jobs, applying, messages, interviews, languages) comes from the website, so site updates
reach the app without a new app release.

## What you need

| For | You need |
|---|---|
| Android | A computer with [Android Studio](https://developer.android.com/studio), a Google Play developer account (US$25, one time) |
| iPhone | A **Mac** with [Xcode](https://developer.apple.com/xcode/), an Apple Developer Program membership (US$99 a year) |
| Both | Node.js 20 or newer, your site live on HTTPS, a free [Firebase](https://console.firebase.google.com) project |

## 1. Install and point the app at your site

```bash
cd mobile
npm install
npm run set-url -- https://your-domain.ph
npx cap sync
```

## 2. Firebase (push notifications)

1. In the Firebase console, create a project, then **Add app**:
   - **Android**, package name `ph.sinagjobs.app`. Download `google-services.json` and put it in
     `mobile/android/app/`.
   - **iOS**, bundle ID `ph.sinagjobs.app`. Download `GoogleService-Info.plist` and drag it into
     `App/App` in Xcode (tick "Copy items if needed").
2. iPhone only: in Firebase, **Project settings → Cloud Messaging → Apple app configuration**, upload an
   APNs key (create it at developer.apple.com → Keys, with "Apple Push Notifications service" ticked).
3. Server: **Project settings → Service accounts → Generate new private key**. On the server, save the
   JSON file as `secrets/fcm.json` inside the `sinag-backend` folder (Docker shows that folder to the app
   as `/srv/secrets`), then set in `.env` and restart:
   ```
   FCM_PROJECT_ID=your-firebase-project-id
   FCM_SERVICE_ACCOUNT_FILE=/srv/secrets/fcm.json
   ```

Don't commit `google-services.json`, `GoogleService-Info.plist` or the service account key to a public repo.

## 3. Build for Android

```bash
npx cap open android
```

In Android Studio: wait for Gradle to finish, plug in a phone (or start an emulator) and press **Run**.
For the Play Store: **Build → Generate Signed App Bundle**, create a keystore (keep it safe: you need the
same one for every update), and upload the `.aab` in the Play Console.

## 4. Build for iPhone (on a Mac)

```bash
npx cap open ios
```

In Xcode, select the **App** target:
- **Signing & Capabilities**: pick your team. Add **Push Notifications** and **Background Modes →
  Remote notifications**.
- Press **Run** to try it on your iPhone. For the App Store: **Product → Archive**, then upload in the Organizer.

## 5. Social sign-in in the app

The app's return link is `ph.sinagjobs.app://auth`. It's already set up in both native projects and on the
server (`APP_SCHEME=ph.sinagjobs.app`). Nothing extra to register with Google, Facebook or LinkedIn: the
sign-in itself still happens on your website's normal redirect URLs.

If you change the app ID, change it in `capacitor.config.json`, `android/app/src/main/res/values/strings.xml`
(`custom_url_scheme`), `ios/App/App/Info.plist` (`CFBundleURLSchemes`) and `APP_SCHEME` in the server's `.env`.

## 6. Store review tips

- Apple rejects apps that are "just a website". This app adds native push, native sign-in hand-off and an
  offline screen; say so in the review notes, and give the reviewer a demo job seeker account.
- Both stores need a privacy policy URL: use `https://your-domain.ph/privacy.html` once it's reviewed.
- Google Play's Data safety form and Apple's privacy labels: you collect name, email, phone (optional),
  resumes, messages and a push token. Payments are test-only for now.
- Payments: Apple and Google generally require their own in-app billing for digital items bought inside an
  app. So the job seeker Extras tab is hidden in both apps, and employers are sent to the website to buy. Check the current App Store and Google Play payment rules before selling
  anything inside the app.

## Updating the app icon or splash screen

Replace the images in `resources/` (1024 × 1024 icon, 2732 × 2732 splash), then:

```bash
npx @capacitor/assets@3.0.5 generate --iconBackgroundColor '#0038A8' --splashBackgroundColor '#0038A8'
npx cap sync
```
