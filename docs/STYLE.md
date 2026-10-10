# Style for text in this repository

Agents read most of this text: tool descriptions, the method, the playbooks, the
skill and the templates. A wrong reading changes what an agent does. Write so
that one reading is possible.

The rules follow ASD-STE100 (Simplified Technical English), at about 70 to 80
percent. We use its sentence rules. We do not use its word list. When a rule
makes a sentence wrong or unclear, keep the sentence and break the rule.

## Rules

1. **Active voice.** Name the actor. Write "The lead checks each line", not "Each
   line is checked".
2. **One instruction per sentence.** Write two sentences, not "do A and then B and
   check C".
3. **Short sentences.** At most 20 words for an instruction and 25 for a
   description. A list is better than a long sentence with three conditions.
4. **Plain verbs.** Write "start", not "spin up". Write "analyze", not "perform an
   analysis of". Do not use phrasal verbs (take off, set up, hand over). Use
   "remove", "configure", "give".
5. **One name for one thing.** Pick a term from the glossary below and use it every
   time. Do not alternate "worker", "agent" and "session" for the same thing.
6. **Say the noun.** Do not drop the subject or the object. Do not write "it" or
   "this" when the reader must look back to find what it means.
7. **No semicolons.** Use two sentences. A comma, a colon or a new sentence does
   the same job as an em dash.
8. **Say what to do and what to check.** An instruction names the action and the
   result a reader can check. Write "the test passes", not "it works well".
9. **No claims of quality.** Do not describe text or code with quality
   adjectives. Give the measurement, or remove the word.
10. **Keep hedges when they are true.** "May have failed" and "failed" are different
    statements. Do not remove a hedge to save words. Do not add one to sound
    careful.
11. **Lists for sequences.** Use a numbered list for three or more steps. Use a
    bullet list for three or more parallel items. Use prose for reasons.
12. **One topic per paragraph.** At most six sentences. Start with the point.
13. **Modal verbs have one sense each.** Use "must" for a rule, "should" for advice
    and "may" for a choice.

## Glossary

| Term | Meaning |
|---|---|
| user | The person the lead works for. |
| lead | The agent that plans work, dispatches it, and checks the results. |
| worker | A long-lived dsh session that does one role of the work. |
| session | The dsh object behind a worker. Use "session" only when the dsh object matters. |
| task | One unit of work with one owner and one worktree. |
| dispatch | The message that gives a task to a worker. |
| acceptance | The checkable lines that say when the worker has finished a task. |
| worktree | The git worktree where the worker builds one task. |
| digest | The summary of what a worker did, from `dsh_digest`. |
| brief | The orientation text the lead gets from `dsh_brief`. |
| preset | A permission level: `read-only`, `supervised`, `autonomous`. |

Add a term here when a new one appears in two or more files.

## Checking

`scripts/ste-lint.py` in the
[asd-ste100-skill](https://github.com/danyuchn/asd-ste100-skill) repository
(MIT licence) checks the sentence rules. Run `python3 ste-lint.py FILE < /dev/null`.
The tool reads standard input when it gets no file, so close stdin. Its count is a
guide. A few findings per 100 words are normal. A finding that changes meaning is
not a fix. Do not use the tool on code.
