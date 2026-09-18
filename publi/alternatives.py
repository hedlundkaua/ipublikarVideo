"""Stable option ordering and labels shared by every quiz surface."""

import hashlib
import random


OPTION_COLUMNS = ("option_a", "option_b", "option_c", "option_d")
VIDEO_QUESTION_COUNT = 4
SUPPORTED_VIDEO_QUESTION_COUNTS = (VIDEO_QUESTION_COUNT, 5)


def option_labels(options, dynamic=True):
    """Use numeric labels only when all options are exactly one letter."""
    if not dynamic:
        return ("A", "B", "C", "D")
    values = [str(option).strip() for option in options]
    if len(values) == 4 and all(len(value) == 1 and value.isalpha() for value in values):
        return ("1", "2", "3", "4")
    return ("A", "B", "C", "D")


def question_labels(question):
    options = [question[column] for column in OPTION_COLUMNS]
    dynamic = question["labels_dynamic"] if "labels_dynamic" in question.keys() else True
    return option_labels(options, dynamic=bool(dynamic))


def _stable_random(*parts):
    seed = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return random.Random(int.from_bytes(hashlib.sha256(seed).digest(), "big"))


def balanced_correct_options(video_id):
    """Return five stable positions containing A-D and no adjacent duplicate."""
    first_four = list(range(4))
    _stable_random("correct-options", video_id).shuffle(first_four)
    possible_fifth = [value for value in range(4) if value != first_four[-1]]
    fifth = _stable_random("correct-option", video_id, 5).choice(possible_fifth)
    return tuple(first_four + [fifth])


def shuffle_question_options(conn, video_id, question_id, position):
    """Persist a question's deterministic order once and only once."""
    question = conn.execute(
        "SELECT * FROM questions WHERE id=?", (question_id,)
    ).fetchone()
    if not question or question["options_shuffled"]:
        return False
    if position not in range(1, max(SUPPORTED_VIDEO_QUESTION_COUNTS) + 1):
        raise ValueError("A posição da questão deve estar entre 1 e 5.")

    options = [question[column] for column in OPTION_COLUMNS]
    correct_value = options[question["correct_option"]]
    incorrect = [option for index, option in enumerate(options)
                 if index != question["correct_option"]]
    _stable_random("question-options", video_id, position).shuffle(incorrect)
    new_correct = balanced_correct_options(video_id)[position - 1]
    reordered = list(incorrect)
    reordered.insert(new_correct, correct_value)
    conn.execute(
        """UPDATE questions
           SET option_a=?,option_b=?,option_c=?,option_d=?,correct_option=?,
               options_shuffled=1
           WHERE id=? AND options_shuffled=0""",
        (*reordered, new_correct, question_id),
    )
    return True
