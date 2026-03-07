# Name Candidates for Open-Sourcing yolo_jail

Evaluation criteria: memorable, short, available on PyPI + GitHub, evocative of the tool's purpose (secure container jail for AI agents), not easily confused with existing tools.

**What this tool does**: Wraps Docker/Podman to create an isolated, pre-configured container environment where AI agents (Copilot, Gemini CLI) can safely run `--yolo` mode without accessing host credentials, SSH keys, or cloud tokens.

---

## Top Tier — Recommended

| # | Name | PyPI | GitHub (mschulkind/) | npm | GitHub org | Justification |
|---|------|------|---------------------|-----|------------|---------------|
| 1 | **yolo-jail** | ✅ | ✅ | ✅ | — | Current name. Already known, descriptive: "YOLO mode, but jailed." Strong brand recognition from internal use. 9 chars. |
| 2 | **stockade** | ✅ | ✅ | ❌ | ⚠️ | Military enclosure/fort. Evocative of containment with strength. Short (8 chars), easy to type. |
| 3 | **bastion** | ✅ | ✅ | ❌ | ⚠️ | Fortified position. Strong, professional. Common enough to be memorable but not overused in CLI tools. 7 chars. |
| 4 | **codejail** | ✅ | ✅ | ✅ | ✅ | Direct: code + jail. Self-documenting. Available everywhere including npm. 8 chars. |
| 5 | **corral** | ✅ | ✅ | ❌ | ⚠️ | Roundup and contain. Western metaphor. Short (6 chars). Implies herding/controlling. |

## Second Tier — Strong Contenders

| # | Name | PyPI | GitHub | npm | Justification |
|---|------|------|--------|-----|---------------|
| 6 | **rampart** | ✅ | ✅ | ❌ | Defensive wall. Strong military/fortress imagery. 7 chars. Conveys protection. |
| 7 | **palisade** | ✅ | ✅ | ❌ | Wall of wooden stakes — primitive but effective defense. 8 chars. Unique in CLI space. |
| 8 | **tether** | ✅ | ✅ | ❌ | Restraint metaphor — agent on a leash. Short (6 chars). Implies controlled freedom. |
| 9 | **holdfast** | ✅ | ✅ | ❌ | Nautical: grip that doesn't let go. Implies persistent containment. 8 chars. |
| 10 | **cagent** | ✅ | ✅ | — | Portmanteau: cage + agent. Clever wordplay. Very short (6 chars). Memorable once explained. |

## Third Tier — Solid Options

| # | Name | PyPI | GitHub | Justification |
|---|------|------|--------|---------------|
| 11 | **shellward** | ✅ | ✅ | Shell + ward (protect). Clear: "guarding the shell." 9 chars. Unix-native feel. |
| 12 | **cloister** | ✅ | ✅ | Enclosed religious community. Implies isolation + purpose. Elegant. 8 chars. |
| 13 | **wardbox** | ✅ | ✅ | Ward (protect) + box (container). Direct compound. 7 chars. |
| 14 | **codebrig** | ✅ | ✅ | Brig = ship's jail. Code + brig. Nautical containment metaphor. 8 chars. |
| 15 | **runguard** | ✅ | ✅ | Run + guard. "Guard your runs." Describes the action perfectly. 8 chars. |
| 16 | **penbox** | ✅ | ✅ | Pen (enclosure) + box (container). Double containment. 6 chars. |
| 17 | **agentcell** | ✅ | ✅ | Agent + cell (prison). Direct and descriptive. 9 chars. |
| 18 | **agentpen** | ✅ | ✅ | Agent + pen (enclosure). Where agents live. 8 chars. |
| 19 | **sandfort** | ✅ | ✅ | Sandbox + fort. Combined metaphors. 8 chars. |
| 20 | **airbrig** | ✅ | ✅ | AI Runtime Brig. Acronym-ish. Unique. 7 chars. |

## Fourth Tier — YOLO-themed

| # | Name | PyPI | GitHub | Justification |
|---|------|------|--------|---------------|
| 21 | **yolojail** | ✅ | ✅ | One word version. Same brand, no hyphen. 8 chars. |
| 22 | **yolocage** | ✅ | ✅ | YOLO + cage. Similar vibe to yolo-jail. 8 chars. |
| 23 | **yolopen** | ✅ | ✅ | YOLO + pen. Short (7 chars). Playful. |
| 24 | **yolobox** | ✅ | ✅ | YOLO + box. Very simple. 7 chars. But GitHub org exists. |
| 25 | **yoloshell** | ✅ | ✅ | YOLO + shell. Describes what you get. 9 chars. GitHub org free. |
| 26 | **yoloden** | ✅ | ✅ | YOLO + den. Cozy containment. 7 chars. |
| 27 | **yolostop** | ✅ | ✅ | YOLO but stopped/contained. Ironic. 8 chars. |

## Fifth Tier — Containment Metaphors

| # | Name | PyPI | GitHub | Justification |
|---|------|------|--------|---------------|
| 28 | **turret** | ✅ | ✅ | Defensive tower. Short (6 chars). Strong visual. |
| 29 | **portcullis** | ✅ | ✅ | Castle gate. Very evocative but long (10 chars). |
| 30 | **paddock** | ✅ | ✅ | Enclosed area for animals. Containment with freedom. 7 chars. |
| 31 | **hutch** | ✅ | ✅ | Small enclosed space. Short (5 chars). Approachable. |
| 32 | **shellpen** | ✅ | ✅ | Shell + pen. Where your shell lives. 8 chars. GitHub org free. |
| 33 | **moatbox** | ✅ | ✅ | Moat + box. Castle defense + container. 7 chars. |
| 34 | **sandward** | ✅ | ✅ | Sandbox + ward. "Guarding the sandbox." 8 chars. |
| 35 | **cellblock** | ✅ | ✅ | Prison section. Strong containment imagery. 9 chars. |

## Sixth Tier — Agent-themed

| # | Name | PyPI | GitHub | Justification |
|---|------|------|--------|---------------|
| 36 | **agentsafe** | ✅ | ✅ | Agent + safe. Direct. 9 chars. GitHub org exists though. |
| 37 | **agentjail** | ✅ | ✅ | Agent + jail. Most literal. 9 chars. GitHub org free. |
| 38 | **agenthold** | ✅ | ✅ | Agent + hold. "Holding" the agent. 9 chars. |
| 39 | **aisafe** | ✅ | — | AI + safe. Very short (6 chars). But vague. |
| 40 | **aipen** | ✅ | — | AI + pen. Short (5 chars). Could be confused with writing. |

## Seventh Tier — Iron/Metal Theme

| # | Name | PyPI | GitHub | Justification |
|---|------|------|--------|---------------|
| 41 | **ironbox** | ✅ | ✅ | Iron + box. Strong, industrial. 7 chars. |
| 42 | **irongate** | ✅ | ✅ | Iron + gate. Access control metaphor. 8 chars. |
| 43 | **ironward** | ✅ | ✅ | Iron + ward. Strong protection. 8 chars. |

## Eighth Tier — Misc Creative

| # | Name | PyPI | GitHub | Justification |
|---|------|------|--------|---------------|
| 44 | **jailbird** | ✅ | ✅ | Slang for prisoner. Playful, memorable. 8 chars. |
| 45 | **jailbox** | ✅ | ✅ | Jail + box. Simple compound. 7 chars. |
| 46 | **codehold** | ✅ | ✅ | Code + hold (ship's cargo area or grip). 8 chars. |
| 47 | **sandcell** | ✅ | ✅ | Sandbox + cell. 8 chars. |
| 48 | **yolodock** | ✅ | ✅ | YOLO + dock(er). Hints at Docker. 8 chars. |
| 49 | **sandbox-ai** | ✅ | ✅ | Descriptive but long with hyphen. 10 chars. |
| 50 | **brig** | ❌ PyPI | ✅ | Ship's jail. Perfect metaphor, shortest possible (4 chars). Sadly taken on PyPI. |

---

## Availability Key

- ✅ = Available (not registered / not found)
- ❌ = Taken (registered package or existing repo)
- ⚠️ = GitHub org/user exists at github.com/name (but mschulkind/name is available)
- — = Not checked / not applicable

All names checked against: PyPI (`pip index versions`), npm (`npm view`), GitHub (`mschulkind/<name>`), GitHub org (`github.com/<name>`).

---

## Selection Criteria

When choosing, consider:

1. **Brevity** — shorter is better for CLI usage (`yolo` is already 4 chars)
2. **Discoverability** — does the name hint at containment/security for AI?
3. **Uniqueness** — will it show up in search results?
4. **Scope resistance** — will the name still fit if features expand?
5. **CLI alias** — what's the natural short alias? (current: `yolo`)
6. **Pronounceability** — is it easy to say in conversation?
7. **Existing brand** — `yolo-jail` is already known in the YOLO jail community of one

## My Recommendation

**Keep `yolo-jail`** — it's already the brand, it's descriptive ("YOLO mode but jailed"), it's available on PyPI and npm, and `yolo` is an excellent CLI name. The name tells you exactly what it does: lets AI agents run in YOLO mode inside a jail.

Runner-up: **stockade** — professional, evocative, short, and completely available. Good if you want a more "serious" name.

Runner-up: **codejail** — the only top-tier name available on ALL platforms (PyPI, npm, GitHub user, GitHub org). Self-documenting.
