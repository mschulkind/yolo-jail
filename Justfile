default:
    @just --list

# Build the docker image using Nix
build:
    nix --extra-experimental-features 'nix-command flakes' build .#dockerImage

# Build and load the image into the local Docker daemon
load: build
    docker load < result

# Run the jail, mounting the current directory to /workspace with user mapping
run:
    docker run --rm -it \
        -v $(pwd):/workspace \
        --user $(id -u):$(id -g) \
        yolo-jail

# Run the jail on a specific target path
run-repo path:
    docker run --rm -it -v {{path}}:/workspace yolo-jail

# Clean up build artifacts
clean:
    rm -f result
