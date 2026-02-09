# Design Document: YOLO Jail

## Objective
Create a secure, stripped-down Docker environment ("YOLO Jail") for AI agents (VS Code Copilot, Gemini) to modify a specific repository without accessing the host filesystem.

## Goals
1.  **Isolation:** The agent operates inside a Docker container.
2.  **Access Control:** Only the target repository is mounted (read/write).
3.  **Tool Restriction:** Dangerous or slow tools (`find`, `grep`) are removed or shimmed. Optimized tools (`fd`, `rg`) are provided.
4.  **Reproducibility:** The environment is defined via `flake.nix`.

## Architecture

### The `flake.nix`
We use Nix Flakes to define the dependencies and build the Docker image.
- **Base:** Minimal Linux (e.g., Alpine or a minimal Nix closure).
- **Packages:**
    - `ripgrep` (rg)
    - `fd`
    - `git`
    - `bash`
    - `coreutils` (potentially restricted subset)
    - `nix` (optional, for development inside)
- **Shims:** Custom scripts for `find` and `grep` that explicitly fail and instruct the agent to use `fd` or `rg`.

### Docker Image
- **Entrypoint:** A shell (bash).
- **Workdir:** `/workspace` (where the host repo is mounted).
- **User:** Non-root user (ideally matching host UID/GID to avoid permission issues, though this requires runtime configuration).

### Shims
Located in `/usr/local/bin` (or similar high-priority path).
- `find`: Prints "Use 'fd' instead." and exits 1.
- `grep`: Prints "Use 'rg' instead." and exits 1.

## Workflow
1.  **Build:** `nix build .#dockerImage`
2.  **Load:** `docker load < result`
3.  **Run:** `docker run -it -v $(pwd):/workspace yolo-jail`

## Future Improvements
- **Network Restrictions:** Block internet access or whitelist specific domains.
- **Resource Limits:** CPU/Memory capping.
- **Audit Logging:** Log all commands executed by the agent.
