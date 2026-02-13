default:
    @just --list

# Build the docker image using Nix
build:
    nix --extra-experimental-features 'nix-command flakes' build .#dockerImage

# Build and load the image into the local Docker daemon
load: build
    docker load < result

# Run the jail on the current directory
run:
    @just run-path $(pwd)

# Run the jail on a specific target path
run-path path *args:
    @mkdir -p ${HOME}/.local/share/yolo-jail/home ${HOME}/.local/share/yolo-jail/mise
    docker run --rm -it \
        -v {{path}}:/workspace \
        -v ${HOME}/.local/share/yolo-jail/home:/home/agent \
        -v ${HOME}/.local/share/yolo-jail/mise:/mise \
        --tmpfs /tmp \
        -e HOME=/home/agent \
        -e XDG_CONFIG_HOME=/home/agent/.config \
        -e MISE_DATA_DIR=/mise \
        -e MISE_CONFIG_DIR=/workspace \
        -e MISE_TRUST=1 \
        -e MISE_YES=1 \
        -e LD_LIBRARY_PATH=/lib:/usr/lib \
        -e PATH=/mise/shims:/bin:/usr/bin \
        -u $(id -u):$(id -g) \
        --env-file <(env | grep -vE '^(SSH_|GIT_|DOCKER_|NIX_)' || true) \
        --workdir /workspace \
        yolo-jail \
        yolo-entrypoint "[[ -f mise.toml ]] && (mise trust && YOLO_BYPASS_SHIMS=1 mise install && YOLO_BYPASS_SHIMS=1 mise upgrade); {{ if args == "" { "bash" } else { args } }}"

# Clean up build artifacts
clean:
    rm -f result
