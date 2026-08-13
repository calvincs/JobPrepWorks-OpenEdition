"""Canned-response provider for keyless development and UI work.

Enable with LLM_PROVIDER=mock. Returns plausible fixed data per schema.
"""

from typing import TypeVar

from pydantic import BaseModel

from app.llm.base import LLMError

T = TypeVar("T", bound=BaseModel)

CANNED: dict[str, dict] = {
    "ProfileExtraction": {
        "facts": [
            {
                "kind": "skill",
                "name": "Python",
                "organization": None,
                "detail": "Backend services and tooling",
                "proficiency": "advanced",
                "start_date": None,
                "end_date": None,
                "evidence_text": "8 years building Python backend services",
            },
            {
                "kind": "role",
                "name": "Senior Software Engineer",
                "organization": "ExampleCorp",
                "detail": "Led a team of 4 on the payments platform",
                "proficiency": None,
                "start_date": "2019-03",
                "end_date": "2024-06",
                "evidence_text": "Senior Software Engineer, ExampleCorp (2019-2024)",
            },
            {
                "kind": "cert",
                "name": "CompTIA Security+",
                "organization": "CompTIA",
                "detail": None,
                "proficiency": None,
                "start_date": "2021-01",
                "end_date": None,
                "evidence_text": "CompTIA Security+ certified, 2021",
            },
        ]
    },
    # No fuzzy matches: mock de-dup relies on the deterministic canonical pass.
    "ProfileReconciliation": {"reconciliations": []},
    "FactParse": {
        "facts": [
            {
                "kind": "role",
                "name": "Data Engineer",
                "organization": "Globex",
                "detail": "Built Spark pipelines on AWS",
                "proficiency": None,
                "start_date": "2019-01",
                "end_date": None,
            },
            {
                "kind": "skill",
                "name": "Spark",
                "organization": None,
                "detail": "Production data pipelines",
                "proficiency": "advanced",
                "start_date": None,
                "end_date": None,
            },
        ]
    },
    "JobExtraction": {
        "title": "Senior Backend Engineer",
        "company": "Acme Corp",
        "location": "Remote (US)",
        "pay_min": 150000,
        "pay_max": 190000,
        "seniority": "senior",
        "sector": "fintech",
        "responsibilities": [
            "Design and operate payment processing services",
            "Mentor junior engineers",
        ],
        "benefits": ["401k match", "Remote-first"],
        "requirements": [
            {"kind": "must", "skill": "python", "level": "5+ years", "evidence_text": "5+ years of Python"},
            {"kind": "must", "skill": "aws", "level": "production experience", "evidence_text": "deep AWS experience required"},
            {"kind": "must", "skill": "postgresql", "level": None, "evidence_text": "strong SQL and PostgreSQL skills"},
            {"kind": "nice", "skill": "kubernetes", "level": None, "evidence_text": "Kubernetes a plus"},
        ],
    },
    "QuestionBank": {
        "questions": [
            {"type": "technical", "skill": "aws", "difficulty": "medium",
             "text": "Walk me through how you would design a highly-available service on AWS.",
             "ideal_answer_criteria": "Should cover multi-AZ deployment, load balancing (ALB), auto-scaling groups, RDS multi-AZ or Aurora, and health checks. Strong answers mention failure modes and cost tradeoffs."},
            {"type": "technical", "skill": "python", "difficulty": "medium",
             "text": "How do you manage dependencies and packaging in a production Python service?",
             "ideal_answer_criteria": "Should cover virtual environments, pinned requirements or lock files, and reproducible builds. Strong answers mention dependency auditing and upgrade strategy."},
            {"type": "technical", "skill": "postgresql", "difficulty": "hard",
             "text": "A query that was fast last month now takes 30 seconds. Walk me through your diagnosis.",
             "ideal_answer_criteria": "Should cover EXPLAIN ANALYZE, index usage, table bloat/vacuum, statistics staleness, and plan changes. Strong answers mention monitoring and query plans over time."},
            {"type": "technical", "skill": "aws", "difficulty": "easy",
             "text": "What is the difference between an AWS security group and a network ACL?",
             "ideal_answer_criteria": "Security groups are stateful and instance-level; NACLs are stateless and subnet-level. Strong answers give a use case for each."},
            {"type": "behavioral", "skill": "mentorship", "difficulty": "medium",
             "text": "Tell me about a time you mentored a struggling engineer.",
             "ideal_answer_criteria": "STAR structure with a specific person/situation, concrete actions taken, and a measurable outcome. Strong answers reflect on what they would do differently."},
            {"type": "behavioral", "skill": "communication", "difficulty": "medium",
             "text": "Describe a time you had to push back on a product decision.",
             "ideal_answer_criteria": "Specific situation, respectful disagreement grounded in data or risk, and a clear resolution. Strong answers show they disagreed and committed."},
            {"type": "situational", "skill": "incident response", "difficulty": "hard",
             "text": "It's 2am and payments are failing for 20% of users. You're on call. What do you do in the first 30 minutes?",
             "ideal_answer_criteria": "Should cover triage, communication/escalation, mitigation before root cause (rollback, feature flag), and evidence gathering. Strong answers mention customer impact framing."},
            {"type": "technical", "skill": "kubernetes", "difficulty": "medium",
             "text": "A pod is stuck in CrashLoopBackOff. How do you debug it?",
             "ideal_answer_criteria": "kubectl describe/logs (including previous container), events, resource limits, liveness probe config. Strong answers distinguish app crash from probe misconfiguration."},
            {"type": "technical", "skill": "python", "difficulty": "hard",
             "text": "Explain how you would profile and fix a memory leak in a long-running Python service.",
             "ideal_answer_criteria": "tracemalloc/objgraph or similar, reproducing under load, common causes (caches, closures, C extensions). Strong answers mention monitoring RSS over time."},
            {"type": "behavioral", "skill": "leadership", "difficulty": "easy",
             "text": "How do you decide what to delegate versus do yourself?",
             "ideal_answer_criteria": "Considers growth opportunities for the team, criticality/risk, and their own leverage. Strong answers give a concrete example."},
        ]
    },
    "AnswerGrade": {
        "score": 4,
        "feedback": "Strong on the core mechanics and you referenced a concrete incident, which grounded the answer. Missing: you never mentioned monitoring or how you would prevent recurrence - a top answer closes the loop with detection and a postmortem action. Tighten the ending: finish with the outcome, not the process.",
    },
    "FollowUpQuestion": {
        "question": "You said you'd look at the 'expensive queries' first - which specific DMV would you actually query to find them, and what one column in an execution plan would tell you an index is missing?",
        "criteria": "Names a concrete DMV (e.g. sys.dm_exec_query_stats joined with sys.dm_exec_sql_text) and cites a specific plan signal (a Table Scan, a Missing Index warning, or a large actual-vs-estimated row difference) rather than staying general.",
    },
    "SessionAssessment": {
        "summary": "A solid but uneven session. Your infrastructure answers were specific and evidence-backed; your behavioral answers drifted into generalities without concrete outcomes. Right now you'd pass a technical screen for this role but risk losing the panel on leadership depth.",
        "per_skill": [
            {"skill": "aws", "comment": "Confident and specific - multi-AZ design answer covered failure modes unprompted."},
            {"skill": "communication", "comment": "Vague. Both answers lacked a measurable outcome; prepare two STAR stories with numbers."},
        ],
        "next_actions": [
            "Write out three STAR stories with measurable outcomes and rehearse them aloud.",
            "Review PostgreSQL query diagnosis (EXPLAIN ANALYZE walkthrough) - it was your weakest technical answer.",
        ],
    },
    "StudyGuideResult": {
        "intro": "You are technically viable for this role but two must-have areas need focused work before an interview: AWS architecture depth and behavioral storytelling. Plan for two focused sessions on each this week.",
        "topics": [
            {"topic": "AWS high-availability patterns", "priority": "high",
             "why_it_matters": "Listed as a must-have and central to the role's day-to-day; your interview scores here average 2.5.",
             "what_to_study": ["Multi-AZ vs multi-region tradeoffs", "ALB + ASG + health check patterns", "RDS/Aurora failover behavior"],
             "how_it_will_be_tested": "Expect a whiteboard-style design question with follow-ups on failure modes and cost."},
            {"topic": "Behavioral stories with outcomes", "priority": "medium",
             "why_it_matters": "Your behavioral answers lack measurable outcomes, which reads as inexperience even when the experience is real.",
             "what_to_study": ["Draft 3 STAR stories with numbers", "Rehearse a 90-second version of each"],
             "how_it_will_be_tested": "Tell me about a time you led/disagreed/failed - with probing follow-ups."},
        ],
    },
    "InsightsResult": {
        "insights": [
            {"kind": "gap", "title": "AWS depth is your biggest cross-job blocker",
             "body": "AWS appears as a must-have in 2 of 2 target jobs, but your profile shows no certification and your interview average on AWS questions is 2.5/5. Closing this one gap improves your standing on every open application.",
             "evidence_skills": ["aws"]},
            {"kind": "strength", "title": "Python is a consistent, evidenced strength",
             "body": "Every target job requires Python and your profile evidences 8 years of production use with a 4.0/5 interview average. Lead with concrete Python war stories - it is your strongest talking point.",
             "evidence_skills": ["python"]},
            {"kind": "sector", "title": "Platform/SRE roles fit your evidenced skills better than pure fintech",
             "body": "Your strongest evidenced skills (Kubernetes, infrastructure, operations) align more with platform engineering postings than the fintech product roles you have been targeting, where domain gaps dominate.",
             "evidence_skills": ["kubernetes", "aws"]},
        ]
    },
    "PitchResult": {
        "pitch_15s": "I'm a backend engineer with eight years of production Python — most recently building payment services — and I'm here because this role's platform scope is exactly where I do my best work.",
        "pitch_30s": "I'm a backend engineer with eight years of Python in production. At my last role I owned the payment pipeline end to end — design, delivery, and the operational load that came with it. What draws me to this role at Acme Corp is the platform ownership: it's the work I keep gravitating toward, and your must-haves line up with what I've actually shipped.",
        "pitch_2min": "I'm a backend engineer, and for the last eight years Python has been my primary tool in production systems. I started out building internal services and grew into owning larger systems — most recently the payment pipeline, where I handled everything from schema design to deployment and the on-call that kept it honest. Along the way I picked up FastAPI and Postgres depth, and I've led small delivery efforts where the hard part was sequencing work across teams, not writing the code. Where I'm honest about growth: AWS is on your must-have list, and while my cloud work has been adjacent, it's the area I'm actively building — I've been deploying personal projects on ECS to close that gap deliberately. What brings me to Acme Corp specifically is the platform ownership in this role. The problems you're describing — reliability, developer experience, scaling the core services other teams build on — are the ones I keep choosing when I have the choice. That's the direction I'm steering my career, and this role sits right on it. I'd bring evidenced Python depth, production judgment, and a track record of owning systems end to end, and I'd be closing the AWS gap from day one with real work rather than tutorials.",
        "talking_points": [
            "Eight years of production Python maps directly to the Python must-have",
            "Payment pipeline ownership demonstrates the end-to-end service ownership the role asks for",
            "ECS side projects show the AWS gap is being closed deliberately",
        ],
    },
    "ResumeResult": {
        "headline": "Senior Backend Engineer — Payments & Platform",
        "summary": "Backend engineer with eight years of production Python experience, most recently owning a payments platform end to end. Strong track record shipping reliable services and mentoring engineers; actively closing the gap on AWS depth this role calls for.",
        "skills": [
            {"category": "Languages & Frameworks", "items": ["Python", "FastAPI"]},
            {"category": "Data", "items": ["PostgreSQL"]},
        ],
        "experience": [
            {
                "title": "Senior Software Engineer",
                "organization": "ExampleCorp",
                "start_date": "2019-03",
                "end_date": "2024-06",
                "bullets": [
                    "Led a team of 4 engineers rebuilding the payments platform",
                    "Owned the payment pipeline end to end, from schema design through on-call operations",
                ],
            }
        ],
        "education": [],
        "certifications": [
            {"name": "CompTIA Security+", "organization": "CompTIA", "date": "2021-01"}
        ],
        "projects": [],
    },
    "FitAnalysisResult": {
        "score": 68,
        "strengths": [
            {"requirement": "python", "evidence": "8 years building Python backend services (advanced)"},
        ],
        "gaps": [
            {"requirement": "aws", "importance": "critical", "why": "No AWS experience evidenced in the profile"},
            {"requirement": "kubernetes", "importance": "minor", "why": "Not evidenced; listed as nice-to-have"},
        ],
        "study_areas": [
            {
                "topic": "AWS core services",
                "why_it_matters": "Listed as a must-have; likely to dominate the technical screen",
                "suggestions": ["AWS Solutions Architect Associate course", "Deploy a small project on ECS + RDS"],
            }
        ],
        # Always present in the canned result; analysis.py stores it only when
        # direction facts actually fed the prompt (the server-side gate).
        "alignment": {
            "verdict": "mixed",
            "summary": "This role advances your platform trajectory, but its on-call load runs against what you said you want less of.",
            "advances": [
                {"aspect": "platform ownership scope", "reason": "Matches your stated move toward platform work"},
            ],
            "conflicts": [
                {"aspect": "production on-call rotation", "reason": "You said you want less on-call churn"},
            ],
        },
    },
    "CompanyPulseOut": {
        "company": "Acme Corp",
        "confidence": 0.72,
        "coverage": {"review_volume": "medium", "news_volume": "medium", "recency": "current"},
        "ratings": {"overall": 4.0, "source": "Glassdoor", "would_recommend_pct": 75},
        "pulse_summary": (
            "Well regarded by engineers; recurring complaints about unclear "
            "priorities since the reorg."
        ),
        "strengths": [
            {"theme": "Engineering culture",
             "detail": "Autonomy and code-quality standards come up repeatedly.",
             "source_id": 1},
        ],
        "complaints": [
            {"theme": "Middle management",
             "detail": "Unclear priorities cited across recent reviews.",
             "source_id": 1},
        ],
        "direction": {
            "summary": "Expanding the payments platform and hiring in infrastructure.",
            "recent_events": [
                {"date": "2026-05", "event": "Payments platform team expansion.",
                 "source_id": 1},
            ],
        },
        "caveats": [],
        "sources": [
            {"id": 1, "title": "Company reviews", "url": "https://example.com/reviews",
             "outlet": "Glassdoor"},
        ],
    },
}

# Company Pulse runs outside get_provider() (server-side web search is
# provider-specific), so its canned result lives here as plain data rather than
# a CANNED schema entry. services/pulse.py substitutes the requested company
# name and stores this instantly when LLM_PROVIDER=mock — no search, no
# network. The CompanyPulseOut entry in CANNED above covers the other path,
# where a caller drives the schema through the normal extract() contract.
CANNED_PULSE: dict = {
    "company": "Acme Corp",
    "confidence": 0.82,
    "coverage": {"review_volume": "high", "news_volume": "medium", "recency": "current"},
    "ratings": {"overall": 4.1, "source": "Glassdoor", "would_recommend_pct": 78},
    "pulse_summary": (
        "Employees rate the company well overall; a strong engineering culture "
        "with growing pains around middle management after last year's reorg."
    ),
    "strengths": [
        {"theme": "Engineering culture",
         "detail": "Consistent praise for autonomy and high code-quality standards.",
         "source_id": 1},
        {"theme": "Compensation",
         "detail": "Pay repeatedly described as at or above market for the sector.",
         "source_id": 1},
    ],
    "complaints": [
        {"theme": "Middle management",
         "detail": "Multiple recent reviews cite unclear priorities since the 2025 reorg.",
         "source_id": 1},
    ],
    "direction": {
        "summary": "Expanding the payments platform and hiring in infrastructure.",
        "recent_events": [
            {"date": "2026-05", "event": "Announced expansion of the payments platform team.",
             "source_id": 2},
        ],
    },
    "caveats": [],
    "sources": [
        {"id": 1, "title": "Company reviews", "url": "https://example.com/reviews",
         "outlet": "Glassdoor"},
        {"id": 2, "title": "Payments platform expansion", "url": "https://example.com/news",
         "outlet": "TechNews"},
    ],
}


class MockProvider:
    def complete(self, *, system: str, prompt: str, max_tokens: int = 16000) -> str:
        return "Mock response."

    def extract(self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 16000) -> T:
        data = CANNED.get(schema.__name__)
        if data is None:
            raise LLMError(f"MockProvider has no canned data for {schema.__name__}")
        return schema.model_validate(data)
