# Vivaran-VQE — System 3 (Full-Stack Web App) Build Spec

## 0. Context — read this first

System 1 (Recommendation Engine) and System 2 (AI Quiz Generator) are **already fully built and working**. Your job is to build **System 3**: the web application shell that ties them together into one coherent user experience — login/register, page flow, and UI — calling System 1's and System 2's existing APIs rather than rebuilding their logic.

Do not modify System 1 or System 2's core logic. Do not rebuild the recommendation engine or the quiz generator. Wire the existing systems together as described below.

---

## 1. Visual design — color scheme (required first step)

Before building any page, inspect the existing frontends:
- System 1's frontend (`frontend/index.html` and any accompanying CSS)
- System 2's frontend (`templates/*.html` and `static/css/style.css` or equivalent, referred to as "PDFQuizzer" in its own docs)

Extract the actual color palette in use (background colors, accent/button colors, text colors, any existing branding) from both. **Use this same palette consistently across every page you build in System 3.** Do not introduce a new, unrelated color scheme — the goal is visual continuity across all three systems, since Systems 1 and 2 already have working, styled frontends.

---

## 2. Landing Page

**Route:** `/`

Centered on the page, top to bottom:
1. **"Vivaran-VQE"** (capital A, rest lowercase, ".ai" lowercase — exact casing matters)
2. A short descriptive paragraph (copy exactly, do not reword):

> Vivaran-VQE is an AI-powered Skill Intelligence Platform built for India's Official Statistical System. It identifies each official's skill gaps against their role, recommends personalized training from a course catalogue, and auto-generates quizzes from real lecture content to verify what's actually been learned — closing the loop between recommended training and real capability.

3. Two buttons: **Login** and **Register**

No other content on this page. No info button here — the info button only appears after login (see Section 6).

---

## 3. Register Flow

Clicking **Register** (from the landing page) opens a **modal/popup** — do NOT navigate to a new page.

**Modal contents:**
- Heading: "Hello user!"
- Field: Username
- Field: Password
- A visible notice text (small, near the password field):
  > Passwords cannot be changed at this time, as the platform is currently in beta. To use a different password, please create a new account.
- Submit on Enter / a submit button

**On successful submission:** close the modal and **redirect to the Login page** (`/login`). Registration does NOT log the user in directly — it always routes through login next.

**Data stored per registered user:** username, password (plaintext or hashed — your choice, this is a hackathon prototype, but do not skip storage). This is the only account data System 3 needs to track for auth purposes.

---

## 4. Login Flow

**Route:** `/login`

Accessible via:
- The **Login** button on the landing page
- Automatically after a successful Register submission

**Fields:** Username, Password. A Login button.

**On submit:**
- **If correct:** redirect to the System 1 page (Section 5).
- **If incorrect:**
  - Clear the password field.
  - Show inline message near the password field: **"Incorrect password. Please try again."**
  - Do not redirect. Let the user retry immediately.
  - After this **first failed attempt**, show a new option beside the Login button: **"Want to register instead?"** — clicking it opens/redirects to the Register flow (Section 3). This option should NOT appear before a failed attempt has occurred, only after.

---

## 5. Global Elements — every page after login

These two elements appear on **every page** once the user is logged in, with no exceptions:

- **Top-left:** the project name "Vivaran-VQE", always visible.
- **Top-right:** a circular **info button** (an avatar/profile-style icon). Clicking it opens a small dropdown with exactly three items:
  1. "Hi, `<username>`!" (display only, not clickable)
  2. **Profile** → navigates to the Profile page (Section 9)
  3. **Logout** → clears the session and redirects to the Landing Page (`/`)

Build this as a shared header/navbar component reused across all post-login pages, not duplicated per page.

---

## 6. System 1 Page — Employee Details & Recommendations

**Route:** `/recommendation` (or similar — this is the page the user lands on immediately after login)

**Important change from System 1's standalone behavior:** the old "register an employee" step is **removed**. The page goes straight into the employee-details intake form — the same fields System 1's existing form/API already expects (designation, department, education, work experience, previous trainings, self-rated skills, etc. — use System 1's existing schema/endpoint as the source of truth for exact fields; do not invent new ones).

Header shown small, top-center of this page: something like "iGOT Recommendation" (understated, not a large hero heading).

**On form submission:**
- Submit to System 1's existing API.
- **Redirect in the same tab** (not a new tab) to the results page (Section 7), passing along the returned employee ID and recommendations.

Info button (Section 5) is present here as on every page.

---

## 7. Recommendation Results Page

Shows:
- The assigned employee ID
- The **top 5 recommended courses** (from System 1's existing response)
- Below **each** of the 5 courses: a clickable link/button labeled **"Take a quiz"**

Info button present here as on every page.

### "Take a quiz" behavior (this is the key integration point)
Clicking "Take a quiz" for a given course:
1. Randomly selects **one link** from **Dataset 6** — a CSV file (containing YouTube links) that you will find inside the System 3 project folder. Access it directly; no need to request it separately.
2. Feeds that selected link into System 2's existing quiz-generation pipeline **automatically** — the user is NOT shown the old "enter lecture material" input step. That step is skipped entirely; the link is supplied programmatically.
3. Redirects (same tab) straight into the quiz page once generation completes.
4. Implementation detail is your choice (server-side random selection + direct call into System 2's pipeline, or however fits your stack) — the requirement is just that the user never manually provides a link or file.

---

## 8. Quiz & Results Page

- User takes the quiz (System 2's existing quiz UI/flow).
- On completion, redirect to a results/analysis page showing their performance (System 2's existing scoring output).
- On this results page, include a professionally worded notice, for example:
  > Please note: each course's quiz can only be attempted once per recommendation cycle.
  (This is a UI-level notice only — no backend enforcement of retake limits is required for this prototype.)
- Include a link/button back to the Recommendation Results Page (Section 7), so the user can return and take quizzes for any of the other 4 recommended courses.
- Info button present here as on every page.

---

## 9. Profile Page

**Route:** `/profile`

Header: **"Hi, `<username>`!"**

Body: a history view of everything this logged-in username has done on the platform:
- Every employee profile this username has submitted through the System 1 intake form (e.g., if they've filled the form 3 times for 3 different employees, show all 3).
- For each employee entry: the details originally submitted, and the courses that were recommended to them.
- For each recommended course: show the quiz score **only if** a quiz was actually attempted for that course. If no quiz was taken for a given course, don't show any quiz-related info for it — omit it entirely rather than showing an empty/placeholder state.

Info button present here as on every page (accessible via the dropdown, consistent with Section 5).

---

## 10. Page/Route Summary

| Route | Page | Auth required | Global header/info button |
|---|---|---|---|
| `/` | Landing page | No | No |
| `/login` | Login | No | No |
| (modal, not a route) | Register | No | No |
| `/recommendation` | Employee intake form | Yes | Yes |
| `/recommendation/results` | Top-5 course recommendations | Yes | Yes |
| `/quiz` | Quiz-taking (System 2) | Yes | Yes |
| `/quiz/results` | Quiz score/analysis | Yes | Yes |
| `/profile` | User's history across all submitted employees | Yes | Yes |

---

## 11. Explicit non-goals for this build

- Do not rebuild System 1 or System 2's core AI logic — call their existing APIs/pipelines.
- Do not implement password-change functionality (explicitly disabled per the beta notice).
- Do not implement backend enforcement of "one quiz attempt per course" — UI notice only.
- Do not invent a new visual design system — reuse the existing System 1/System 2 palette (Section 1).
- Do not add an info button to the Landing Page or Login page — those are pre-login only.
