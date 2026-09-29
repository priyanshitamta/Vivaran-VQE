import json
import random

QUESTION_BANK = "question_bank/questions.json"


def load_questions():
    with open(QUESTION_BANK, "r", encoding="utf-8") as file:
        questions = json.load(file)
    if len(questions) == 1 and isinstance(questions[0], list):
        return questions[0]
    return questions


def get_questions_for_lecture(all_questions, lecture_number, questions_per_lecture):
    start = (lecture_number - 1) * questions_per_lecture
    end = start + questions_per_lecture

    return all_questions[start:end]


def get_remaining_questions(all_questions, used_question_ids):
    return [
        question
        for question in all_questions
        if question["id"] not in used_question_ids
    ]


def select_final_questions(all_questions, used_question_ids):
    remaining = get_remaining_questions(all_questions, used_question_ids)
    random.shuffle(remaining)
    return remaining