import json
import os
from difflib import SequenceMatcher

from dotenv import load_dotenv
from langchain.prompts import PromptTemplate
from langchain_groq import ChatGroq
from langchain_core.output_parsers import StrOutputParser

load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")


def generate_mcqs(summary_text, num_questions, topic=None):
    with open("prompts/mcq_prompt.txt") as f:
        template = f.read()

    # The prompt contains a JSON example. Escape its literal braces so
    # LangChain only interprets the three values supplied to the chain.
    template = template.replace("{", "{{").replace("}", "}}")
    template += (
        "\n\nLecture transcript:\n{context}"
        "\n\nRequested number of questions: {num_questions}"
        "\nLecture topic (if provided): {topic}"
        "\n\nIMPORTANT OUTPUT RULES:"
        "\n- Return ONLY a single valid JSON array of question objects."
        "\n- Do NOT wrap the JSON in markdown code fences."
        "\n- Do NOT include any explanation, commentary, or text before or after the JSON."
        "\n- Ensure the JSON is complete and properly closed (no truncated strings, "
        "no trailing commas)."
        "\n- Every string value must be fully closed with a matching quote."
    )

    prompt = PromptTemplate(
        input_variables=["context", "num_questions", "topic"],
        template=template,
    )

    # max_tokens was previously unset (defaulting too low), which caused the
    # model's JSON output to get cut off mid-string ("Unterminated string" errors).
    # Raised here so a full question bank can be returned without truncation.
    llm = ChatGroq(
        api_key=GROQ_API_KEY,
        model_name="openai/gpt-oss-20b",
        temperature=0.3,
        max_tokens=8192,
    )

    chain = prompt | llm | StrOutputParser()

    result = chain.invoke({
        "context": summary_text,
        "num_questions": num_questions,
        "topic": topic or ""
    })

    return result


def validate_mcqs(questions, transcript):
    """Drop questions that are structurally broken or not grounded in the transcript."""
    transcript_lower = transcript.lower()
    seen_questions = set()
    valid = []

    for q in questions:
        # answer must literally be one of the options
        if q.get("answer") not in q.get("options", []):
            continue

        # no duplicate questions
        qtext = q.get("question", "").strip().lower()
        if not qtext or qtext in seen_questions:
            continue
        seen_questions.add(qtext)

        # source_snippet must actually resemble something in the transcript
        snippet = q.get("source_snippet", "").strip().lower()
        if snippet and len(transcript_lower) > 20:
            best_ratio = max(
                SequenceMatcher(
                    None, snippet, transcript_lower[i:i + len(snippet) + 20]
                ).ratio()
                for i in range(0, max(len(transcript_lower) - len(snippet), 1), 40)
            )
            if snippet not in transcript_lower and best_ratio < 0.6:
                q["grounding_warning"] = True

        valid.append(q)

    return valid


def verify_mcqs(questions, transcript):
    """Ask the LLM to fact-check its own questions against the transcript.

    Every question that comes out of this function now carries an explicit
    verification_status ("verified" / "flagged" / "unverified") and, where
    relevant, a verification_reason explaining why. Previously this function
    only filtered out bad IDs without tagging the survivors, so every
    question fell back to the "unverified" default in app.py regardless of
    whether it had actually been checked.
    """
    if not questions:
        return questions

    llm = ChatGroq(
        api_key=GROQ_API_KEY,
        model_name="openai/gpt-oss-20b",
        temperature=0,
        max_tokens=4096,
    )

    check_prompt = (
        "You are a strict fact-checker. Below is a lecture transcript and a list of "
        "MCQ questions generated from it. For each question, verify that the marked "
        "answer is actually correct according to the transcript, and that the question "
        "is not about something outside the transcript.\n\n"
        f"Transcript:\n{transcript}\n\n"
        f"Questions (JSON):\n{json.dumps(questions)}\n\n"
        "Return ONLY a JSON array of the ids of questions that are INACCURATE or "
        "NOT SUPPORTED by the transcript, e.g. [2, 5]. Return [] if all are accurate."
    )

    try:
        result = llm.invoke(check_prompt).content
        cleaned = result.strip().strip("`")
        bad_ids = set(json.loads(cleaned))
    except (json.JSONDecodeError, ValueError, Exception):
        # The fact-check call itself failed or returned unparsable output.
        # Don't pretend we verified anything — tag everything as unverified
        # and say why, instead of silently leaving verification_status unset.
        for q in questions:
            q["verification_status"] = "unverified"
            q["verification_reason"] = (
                "Automated fact-check could not be completed or its response "
                "could not be parsed."
            )
        return questions

    checked = []
    for q in questions:
        if q["id"] in bad_ids:
            q["verification_status"] = "flagged"
            q["verification_reason"] = (
                "Fact-check found this question's answer inaccurate or "
                "unsupported by the transcript."
            )
            # Kept (not dropped) so the "Flagged" count in the UI is meaningful.
            # If you'd rather remove flagged questions entirely, use
            # `continue` here instead of appending.
        else:
            q["verification_status"] = "verified"
            q["verification_reason"] = ""
            # Surface validate_mcqs()'s grounding warning too, if present and
            # not already overridden above.
            if q.get("grounding_warning"):
                q["verification_status"] = "flagged"
                q["verification_reason"] = (
                    "Source snippet does not closely match the transcript."
                )
        checked.append(q)

    return checked