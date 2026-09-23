from __future__ import annotations

_MIN_FRAGMENT_LEN = 40  # достаточно длинный кусок, чтобы не ловить случайные совпадения


def _significant_lines(prompt: str) -> list[str]:
    return [line.strip() for line in prompt.splitlines() if len(line.strip()) >= _MIN_FRAGMENT_LEN]


def find_leaked_system_prompt_fragments(outputs: list[str | None], system_prompts: list[str]) -> list[str]:
    """Return the system-prompt lines that verbatim leaked into any of `outputs`.

    This is a cheap, deterministic last line of defense (layer 3 in the
    threat model): it does not try to detect paraphrased leaks, only
    near-verbatim reproduction of our own instructions in the model output.
    """
    hits: list[str] = []
    combined_output = "\n".join(o for o in outputs if o)
    if not combined_output:
        return hits

    for prompt in system_prompts:
        for line in _significant_lines(prompt):
            if line in combined_output:
                hits.append(line[:80])

    return hits
