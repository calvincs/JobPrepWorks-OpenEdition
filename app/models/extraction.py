"""Pydantic schemas for the LLM structured-output pipelines.

Every pipeline validates against one of these: the provider is asked for
this shape and the result is parsed into it before anything is stored, so a
model that free-associates fails loudly instead of writing junk to the
database. Adding a pipeline starts here — see CLAUDE.md.
"""

from typing import Literal

from pydantic import BaseModel, Field


class ExtractedFact(BaseModel):
    kind: Literal["skill", "role", "education", "cert", "project"]
    name: str = Field(description="Job TITLE for a role (e.g. 'Senior SRE'), or the skill/cert/degree name")
    organization: str | None = Field(
        default=None,
        description="Employer for a role, issuing body for a cert, institution for education; null otherwise",
    )
    detail: str | None = Field(default=None, description="One-line elaboration")
    proficiency: Literal["beginner", "intermediate", "advanced", "expert"] | None = None
    start_date: str | None = Field(default=None, description="YYYY-MM if known")
    end_date: str | None = Field(default=None, description="YYYY-MM, or null if current")
    evidence_text: str = Field(description="Short quote from the document supporting this fact")


class ProfileExtraction(BaseModel):
    facts: list[ExtractedFact]


class ParsedFact(BaseModel):
    """A career fact parsed from the user's freeform self-description. Unlike
    ExtractedFact there is no document behind it, so no evidence quote."""

    kind: Literal["skill", "role", "education", "cert", "project"]
    name: str = Field(description="Job TITLE for a role (e.g. 'Senior SRE'), or the skill/cert/degree name")
    organization: str | None = Field(
        default=None,
        description="Employer for a role, issuing body for a cert, institution for education; null otherwise",
    )
    detail: str | None = Field(default=None, description="One-line elaboration in the person's words")
    proficiency: Literal["beginner", "intermediate", "advanced", "expert"] | None = None
    start_date: str | None = Field(default=None, description="YYYY-MM if known")
    end_date: str | None = Field(default=None, description="YYYY-MM, or null if current")


class FactParse(BaseModel):
    facts: list[ParsedFact]


class ReconciledFact(BaseModel):
    """Maps a newly-extracted fact to an existing one it duplicates (de-dup)."""

    new_index: int = Field(description="0-based index into the NEW UNMATCHED FACTS list")
    match: int | None = Field(
        default=None,
        description="id of the EXISTING fact this duplicates; null if it is genuinely new",
    )


class ProfileReconciliation(BaseModel):
    reconciliations: list[ReconciledFact]


class ExtractedRequirement(BaseModel):
    kind: Literal["must", "nice"]
    skill: str = Field(description="Canonical short skill name, e.g. 'aws', 'python', 'kubernetes'")
    level: str | None = Field(default=None, description="Expected proficiency/experience, if stated")
    evidence_text: str = Field(description="Short quote from the posting")


class JobExtraction(BaseModel):
    title: str | None = None
    company: str | None = None
    location: str | None = None
    pay_min: int | None = Field(default=None, description="Annual, in posting currency")
    pay_max: int | None = None
    seniority: str | None = Field(default=None, description="e.g. junior, mid, senior, staff, lead")
    sector: str | None = Field(default=None, description="Industry sector, e.g. fintech, healthcare")
    responsibilities: list[str]
    benefits: list[str]
    requirements: list[ExtractedRequirement]


class FitStrength(BaseModel):
    requirement: str
    evidence: str = Field(description="The profile fact(s) that demonstrate this requirement is met")


class FitGap(BaseModel):
    requirement: str
    importance: Literal["critical", "important", "minor"]
    why: str = Field(description="Why the profile does not evidence this requirement")


class StudyArea(BaseModel):
    topic: str
    why_it_matters: str
    suggestions: list[str] = Field(description="Concrete study actions")


class AlignmentPoint(BaseModel):
    aspect: str = Field(description="The specific job attribute (e.g. 'staff-level scope', 'heavy on-call rotation')")
    reason: str = Field(description="Why this advances or conflicts with the candidate's stated direction, citing their words")


class FitAlignment(BaseModel):
    verdict: Literal["aligned", "mixed", "misaligned"]
    summary: str = Field(description="One-to-two sentence overall read of the job against the stated direction")
    advances: list[AlignmentPoint]
    conflicts: list[AlignmentPoint]


class FitAnalysisResult(BaseModel):
    score: int = Field(ge=0, le=100, description="Calibrated fit score; unsupported skills are gaps")
    strengths: list[FitStrength]
    gaps: list[FitGap]
    study_areas: list[StudyArea]
    alignment: FitAlignment | None = Field(
        default=None,
        description="Only when a CAREER DIRECTION block was provided; null otherwise",
    )


class PitchResult(BaseModel):
    pitch_15s: str = Field(description="~40 spoken words: hook + strongest evidenced match to this job")
    pitch_30s: str = Field(description="~80 spoken words: present -> proof -> why this role")
    pitch_2min: str = Field(description="~280-320 spoken words: present -> past evidence against the must-haves -> why this job/company now")
    talking_points: list[str] = Field(description="3-5 bridge lines connecting specific profile facts to specific must-haves, for improvising")


class ResumeExperienceEntry(BaseModel):
    title: str = Field(description="Job title as it should appear on the resume")
    organization: str | None = Field(default=None, description="Employer name")
    start_date: str | None = Field(default=None, description="YYYY-MM if known, else null")
    end_date: str | None = Field(default=None, description="YYYY-MM, or null if current/ongoing")
    bullets: list[str] = Field(
        description="2-5 resume bullet points for this role, each grounded in an "
        "evidenced profile fact, achievement-oriented and quantified only where the "
        "evidence supports a number"
    )


class ResumeEducationEntry(BaseModel):
    institution: str | None = None
    degree: str = Field(description="Degree/program name")
    start_date: str | None = Field(default=None, description="YYYY-MM if known")
    end_date: str | None = Field(default=None, description="YYYY-MM, or null if ongoing")
    detail: str | None = Field(
        default=None, description="One-line elaboration (honors, focus) only if evidenced"
    )


class ResumeCertification(BaseModel):
    name: str
    organization: str | None = Field(default=None, description="Issuing body")
    date: str | None = Field(default=None, description="YYYY-MM issued, if known")


class ResumeProject(BaseModel):
    name: str
    detail: str | None = None
    bullets: list[str] = Field(default_factory=list, description="1-3 bullets on impact/tech used")


class ResumeSkillGroup(BaseModel):
    category: str = Field(
        description="Grouping label chosen to fit the candidate's evidenced skills, "
        "e.g. 'Languages', 'Cloud & Infra', 'Tools'"
    )
    items: list[str]


class ResumeResult(BaseModel):
    headline: str = Field(
        description="One-line professional headline tailored to this job, "
        "e.g. 'Senior Backend Engineer — Payments & Platform'"
    )
    summary: str = Field(
        description="3-4 sentence professional summary tailored to this job, "
        "grounded in evidenced facts, no invented experience"
    )
    skills: list[ResumeSkillGroup]
    experience: list[ResumeExperienceEntry]
    education: list[ResumeEducationEntry]
    certifications: list[ResumeCertification] = Field(default_factory=list)
    projects: list[ResumeProject] = Field(default_factory=list)


class GeneratedQuestion(BaseModel):
    type: Literal["technical", "behavioral", "situational"]
    skill: str = Field(description="Canonical short name of the skill/competency this question probes")
    difficulty: Literal["easy", "medium", "hard"]
    text: str = Field(description="The question, phrased as an interviewer would ask it")
    ideal_answer_criteria: str = Field(
        description="What a strong answer must cover; used later to grade the candidate's answer"
    )


class QuestionBank(BaseModel):
    questions: list[GeneratedQuestion]


class AnswerGrade(BaseModel):
    score: int = Field(ge=1, le=5, description="Calibrated 1-5; a mediocre answer scores 3, not 4")
    feedback: str = Field(
        description="Warm, specific coaching addressed to 'you': what you did well, "
        "what was missing, and what a top answer would include"
    )


class FollowUpQuestion(BaseModel):
    """A single probing follow-up an interviewer asks after a weak answer, to give
    the candidate a second chance to reach the specifics the question was after."""

    question: str = Field(
        description="One focused follow-up addressed to 'you', narrowing in on the "
        "biggest gap in the previous answer. Ask for the specific detail; may give a "
        "small hint to steer, but must not hand over the answer."
    )
    criteria: str = Field(
        description="What a strong response to THIS follow-up should include — the "
        "grading rubric for the follow-up answer, concrete and specific."
    )


class SkillAssessment(BaseModel):
    skill: str
    comment: str = Field(description="Honest one-to-two sentence verdict on this skill's showing")


class SessionAssessment(BaseModel):
    summary: str = Field(description="Honest overall assessment of the session")
    per_skill: list[SkillAssessment]
    next_actions: list[str] = Field(description="2-3 concrete next actions")


class StudyTopic(BaseModel):
    topic: str
    priority: Literal["high", "medium", "low"]
    why_it_matters: str = Field(description="Why this matters for THIS job")
    what_to_study: list[str] = Field(description="Concrete study items")
    how_it_will_be_tested: str = Field(description="How an interview will likely probe it")


class StudyGuideResult(BaseModel):
    intro: str = Field(description="Short framing of where the candidate stands and the plan")
    topics: list[StudyTopic]


class InsightItem(BaseModel):
    kind: Literal["gap", "strength", "sector"]
    title: str = Field(description="Short headline, e.g. 'AWS certification is your biggest blocker'")
    body: str = Field(description="2-4 sentences grounded in the provided data, with numbers")
    evidence_skills: list[str] = Field(
        description="Canonical skill names from the data this insight is grounded in"
    )


class InsightsResult(BaseModel):
    insights: list[InsightItem]


# ── Company Pulse (services/pulse.py) ────────────────────────────────────────
# Used by the research path that searches the web from this app and hands the
# results to the model: a real schema means Ollama can enforce the shape
# server-side and every provider gets validation for free. The providers that
# search server-side return prose instead and are validated by
# pulse.validate_pulse(), which enforces the same shape plus URL sanitizing.


class PulseCoverage(BaseModel):
    review_volume: Literal["high", "medium", "low", "none"] = "none"
    news_volume: Literal["high", "medium", "low", "none"] = "none"
    recency: Literal["current", "dated", "stale"] = "stale"


class PulseRatings(BaseModel):
    overall: float | None = Field(default=None, description="Out of 5, or null if unknown")
    source: str | None = Field(default=None, description="Where the rating came from")
    would_recommend_pct: int | None = None


class PulseSource(BaseModel):
    id: int = Field(description="Citation number referenced by source_id elsewhere")
    title: str
    url: str
    outlet: str = Field(default="", description="Publication or site name")


class PulseItem(BaseModel):
    theme: str = Field(description="Two or three words naming the theme")
    detail: str = Field(description="Under 40 words, specific, no padding")
    source_id: int | None = Field(default=None, description="id of the source this rests on")


class PulseEvent(BaseModel):
    date: str = Field(default="", description="YYYY-MM or YYYY if known")
    event: str
    source_id: int | None = None


class PulseDirection(BaseModel):
    summary: str = Field(default="", description="Under 50 words: where the company is heading")
    recent_events: list[PulseEvent] = Field(default_factory=list)


class CompanyPulseOut(BaseModel):
    company: str = Field(description="Canonical name of the employer researched")
    confidence: float = Field(description="0-1: how well-evidenced this pulse is")
    coverage: PulseCoverage = Field(default_factory=PulseCoverage)
    ratings: PulseRatings = Field(default_factory=PulseRatings)
    pulse_summary: str = Field(description="60 words or fewer: the honest one-liner")
    strengths: list[PulseItem] = Field(default_factory=list)
    complaints: list[PulseItem] = Field(default_factory=list)
    direction: PulseDirection = Field(default_factory=PulseDirection)
    caveats: list[str] = Field(
        default_factory=list, description="Ambiguity, thin data, conflicting sources"
    )
    sources: list[PulseSource] = Field(default_factory=list)
