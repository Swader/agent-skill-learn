# Card quality

Good cards are small, stable, and useful under pressure.

## Convert sources into cards

- Extract decisions, invariants, danger zones, vocabulary, owners, and "how not
  to break it" rules.
- Prefer "why does this matter?" answers over bare definitions.
- Keep answers short enough to compare against a user's recall in one pass.
- Add the source path or URL so stale cards can be repaired later.
- Make cards operational: a future user should answer faster, debug safer, or
  communicate with less ambiguity.

## Avoid

- Trivia that can be searched in seconds.
- Cards with multiple unrelated facts.
- Questions whose answer is "it depends" unless the card teaches the decision
  rule.
- Duplicates with slightly different wording.
- Active cards that contradict newer cards.
- Long excerpts from copyrighted or private material when a summary is enough.

## Useful card patterns

```text
What owns X?
Why does X bypass Y?
What should you check before changing X?
What does PERSON optimize for?
How do you reduce entropy when asking PERSON about TOPIC?
What is the difference between A and B?
What would break if ASSUMPTION is false?
```
