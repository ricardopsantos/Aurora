# Aurora — Requirements

Testable behavioral rules, one per numbered entry, in the same `R<n>` numbering
already used throughout the codebase's comments and commit messages. This
file didn't exist before R223 — earlier requirements live only as code
comments and commit history; new ones from here on get an entry too.

**Before adding one, take the next number from `documents/CHANGELOG_TECHNICAL.md`
— the canonical spec — and from `git log`, never from this file and never
from a code grep.** Both of the obvious shortcuts are wrong, and both were
tried here on 2026-08-26 and produced collisions:

- *This file* documented only R223 while R220–R228 were already allocated.
- *A code grep* misses any requirement whose fix left no `R<n>` string in the
  source. R224 (Ollama remote-API status) and R225 (mdrender trailing
  newline) are real, committed requirements with no marker in any `.py`.

`git log --format=%s | grep -oE 'R[0-9]+' | sort -t R -k2 -n | tail -1` is
the reliable high-water mark, because every requirement lands in a commit
whose subject names it. R227 and R228 (in `requirements/`) were back-filled from their
code comments after such a collision.

## Where each requirement lives

This file is the index. The requirements themselves are split by area into
`requirements/`, five files at most. **Numbers are global and never change
when an entry moves**: code, tests and commits cite them. To find one, grep
for its number under `requirements/`. A new requirement takes the next free
number and goes into the file whose area matches its main behaviour. Add its number to the
list below too, so the index stays complete.

| File | Area | Entries |
|---|---|---|
| [`requirements/providers.md`](requirements/providers.md) | Provider configuration, endpoints, keys, model picker, context limits, fallbacks, idle-unloaded servers. | 10 |
| [`requirements/tools-and-approvals.md`](requirements/tools-and-approvals.md) | Model tools (edit, patch, run_command, web_fetch, find_files), approval gates, interpreters, extensions, MCP, git/commit. | 29 |
| [`requirements/tui-and-commands.md`](requirements/tui-and-commands.md) | The terminal UI, Esc/cancel behaviour, slash commands, clipboard, input box, one-shot mode. | 17 |
| [`requirements/sessions-and-storage.md`](requirements/sessions-and-storage.md) | Session records, checkpoints and the shadow repo, atomic writes, compaction, history. | 13 |
| [`requirements/performance-robustness.md`](requirements/performance-robustness.md) | Bounded work, background jobs, frame cost, dead endpoints, review passes and audit fix batches. | 8 |

## Number → file

- **Providers & models** (`providers.md`): R223, R228, R233, R267, R278, R295, R315, R316, R297, R307, R308, R325, R326, R341, R343, R344, R345
- **Tools, edits & approvals** (`tools-and-approvals.md`): R227, R230, R231, R236, R238, R239, R242, R243, R244, R245, R247, R262, R265, R268, R269, R270, R271, R273, R277, R281, R282, R283, R284, R285, R287, R292, R309, R310, R319, R320, R321, R322, R323, R324, R327, R328, R329, R334, R335, R336, R338, R339, R340, R350
- **TUI & commands** (`tui-and-commands.md`): R235, R253, R254, R255, R256, R258, R259, R260, R261, R263, R264, R272, R279, R288, R311, R312, R313, R314, R317, R318, R330, R331, R332, R333, R337, R342, R347, R348, R349, R351, R352, R353, R354, R355, R356, R357, R358, R359, R360, R361, R362, R363, R364, R365, R366, R367, R368, R369, R370, R371, R372, R373, R374, R375, R376, R377, R378, R379, R380, R381, R382, R383, R384, R385, R386, R387, R388, R389, R390, R391, R392, R394, R395, R396, R397, R398, R399, R400, R401, R402, R403, R404, R405, R406, R407, R408, R409, R410, R411, R413, R414, R415, R416, R417, R418, R419, R420, R421, R422, R423, R424, R425, R426, R427, R428, R429, R430, R431, R432, R433, R434, R435, R436, R437, R438, R439, R440
- **Sessions, checkpoints & storage** (`sessions-and-storage.md`): R229, R232, R234, R237, R240, R241, R274, R275, R276, R280, R286, R291, R296
- **Performance & robustness** (`performance-robustness.md`): R246, R257, R266, R289, R290, R293, R294, R298, R346, R393, R441
