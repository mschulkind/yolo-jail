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
run-path path:
    @mkdir -p .home .mise-cache
    docker run --rm -it \
        -v {{path}}:/workspace \
        -v $(pwd)/.home:/home/agent \
        -v $(pwd)/.mise-cache:/mise \
        -v ${HOME}/.config/gh:/home/agent/.config/gh:ro \
        -v ${HOME}/.config/gemini-cli:/home/agent/.config/gemini-cli:ro \
        -v ${HOME}/.config/gcloud:/home/agent/.config/gcloud:ro \
        -e HOME=/home/agent \
        -e MISE_DATA_DIR=/mise \
        -e MISE_CONFIG_DIR=/workspace \
        -e MISE_TRUST=1 \
        -e MISE_YES=1 \
        -e PATH=/mise/shims:/bin:/usr/bin \
        --user $(id -u):$(id -g) \
        --workdir /workspace \
        yolo-jail \
        bash -c "[[ -f mise.toml ]] && mise install; bash"

# Clean up build artifacts
clean:
    rm -f result
