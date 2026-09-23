from __future__ import annotations

_MIN_FRAGMENT_LEN = 40


def _significant_lines(prompt: str) -> list[str]:
    return [line.strip() for line in prompt.splitlines() if len(line.strip()) >= _MIN_FRAGMENT_LEN]


def find_leaked_system_prompt_fragments(outputs: list[str | None], system_prompts: list[str]) -> list[str]:
    hits: list[str] = []
    combined_output = "\n".join(o for o in outputs if o)
    if not combined_output:
        return hits

    for prompt in system_prompts:
        for line in _significant_lines(prompt):
            if line in combined_output:
                hits.append(line[:80])

    return hits
