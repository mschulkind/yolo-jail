{
  description = "YOLO Jail: A restricted Docker environment for AI agents";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        # Extra packages from project config (passed via YOLO_EXTRA_PACKAGES env var)
        extraPackageNames = let
          raw = builtins.getEnv "YOLO_EXTRA_PACKAGES";
        in
          if raw == "" then [] else builtins.fromJSON raw;

        extraPackages = map (name: pkgs.${name}) extraPackageNames;

        # Derivation for the shim scripts
        shims = pkgs.stdenv.mkDerivation {
          name = "yolo-shims";
          src = ./src/shims;
          installPhase = ''
            mkdir -p $out/bin
            cp * $out/bin/
            chmod +x $out/bin/*
          '';
        };

        # Derivation to provide /usr/bin/env and other standard paths
        binPathLinks = pkgs.runCommand "bin-path-links" {} ''
          mkdir -p $out/usr/bin $out/bin $out/lib64 $out/lib $out/usr/lib $out/etc $out/usr/share/fonts
          ln -s ${pkgs.coreutils}/bin/env $out/usr/bin/env
          ln -s ${pkgs.bashInteractive}/bin/bash $out/bin/bash
          ln -s ${pkgs.bashInteractive}/bin/sh $out/bin/sh
          ln -s ${pkgs.gawk}/bin/awk $out/bin/awk
          ln -s ${pkgs.gnused}/bin/sed $out/bin/sed
          ln -s ${pkgs.gnugrep}/bin/grep $out/bin/grep
          ln -s ${pkgs.findutils}/bin/find $out/bin/find
          ln -s ${pkgs.chromium}/bin/chromium $out/usr/bin/chromium
          ln -s ${pkgs.chromium}/bin/chromium $out/usr/bin/google-chrome
          ln -s ${pkgs.chromium}/bin/chromium $out/usr/bin/chrome
          
          # Link the dynamic linker for x86_64
          ln -s ${pkgs.stdenv.cc.bintools.dynamicLinker} $out/lib64/ld-linux-x86-64.so.2
          ln -s ${pkgs.fontconfig.out}/etc/fonts $out/etc/fonts
          
          # Link shared libraries to /lib and /usr/lib for LD_LIBRARY_PATH discovery.
          # Iterates over all packages with lib outputs, including split-output packages
          # (e.g., fontconfig.lib has .so files separate from fontconfig.out which has etc/).
          # Note: glib and pango define outputs=["bin" "out" ...] so their DEFAULT output
          # is "bin" (no lib/). Must use .out explicitly to get the libraries.
          # Non-nix binaries (node, npm/pip packages) rely on LD_LIBRARY_PATH=/lib:/usr/lib
          # since they lack RPATH entries pointing into the nix store.
          for dir in $out/lib $out/usr/lib; do
            for pkg in ${pkgs.glibc} \
                       ${pkgs.stdenv.cc.cc.lib} \
                       ${pkgs.zlib} \
                       ${pkgs.fontconfig.lib} \
                       ${pkgs.glib.out} \
                       ${pkgs.pango.out} \
                       ${pkgs.cairo} \
                       ${pkgs.harfbuzz} \
                       ${pkgs.freetype} \
                       ${pkgs.fribidi} \
                       ${pkgs.pixman} \
                       ${pkgs.libpng} \
                       ${pkgs.expat} \
                       ${pkgs.pcre2} \
                       ${pkgs.libffi}; do
              if [ -d "$pkg/lib" ]; then
                for f in "$pkg"/lib/lib*.so*; do
                  [ -f "$f" ] || [ -L "$f" ] || continue
                  name=$(basename "$f")
                  [ ! -e "$dir/$name" ] && ln -s "$f" "$dir/$name" 2>/dev/null || true
                done
              fi
            done
          done

          # Font directories: symlink into /usr/share/fonts so fontconfig finds them
          # (fontconfig's default fonts.conf includes <dir>/usr/share/fonts</dir>)
          # Note: do NOT symlink freefont_ttf — its fonts lack fontconfig classification
          # and 49-sansserif.conf misclassifies FreeMono as sans-serif, beating DejaVu.
          # dejavu-fonts-minimal (transitive dep) handles base fonts correctly.
          for fontPkg in ${pkgs.noto-fonts-color-emoji}; do
            if [ -d "$fontPkg/share/fonts" ]; then
              for d in "$fontPkg"/share/fonts/*; do
                [ -d "$d" ] && ln -s "$d" "$out/usr/share/fonts/$(basename "$d")" 2>/dev/null || true
              done
            fi
          done

          # Podman nested container support
          echo "root:100000:65536" > $out/etc/subuid
          echo "root:100000:65536" > $out/etc/subgid

          # Podman storage config for rootless operation
          mkdir -p $out/etc/containers
          cat > $out/etc/containers/storage.conf <<STORAGE
          [storage]
          driver = "overlay"
          [storage.options.overlay]
          mount_program = "${pkgs.fuse-overlayfs}/bin/fuse-overlayfs"
          STORAGE

          cat > $out/etc/containers/containers.conf <<CONTAINERS
          [containers]
          cgroups = "disabled"
          default_sysctls = []
          [network]
          default_rootless_network_cmd = "slirp4netns"
          [engine]
          cgroup_manager = "cgroupfs"
          events_logger = "file"
          CONTAINERS

          cat > $out/etc/containers/policy.json <<POLICY
          {"default":[{"type":"insecureAcceptAnything"}]}
          POLICY

          cat > $out/etc/containers/registries.conf <<REGISTRIES
          unqualified-search-registries = ["docker.io"]
          REGISTRIES
        '';

        # Derivation for the Python entrypoint
        entrypointScript = pkgs.writeTextFile {
          name = "yolo-entrypoint-py";
          text = builtins.readFile ./src/entrypoint.py;
          destination = "/lib/yolo-entrypoint.py";
        };
        entrypoint = pkgs.writeShellScriptBin "yolo-entrypoint" ''
          exec ${pkgs.python313}/bin/python3 ${entrypointScript}/lib/yolo-entrypoint.py "$@"
        '';

        # In-jail yolo CLI wrapper — delegates to the mounted repo via uv
        yoloCli = pkgs.writeShellScriptBin "yolo" ''
          # Use the mounted repo with uv (deps are cached in persistent ~/.cache/uv)
          if [ -d /opt/yolo-jail/src ]; then
            export PYTHONPATH="/opt/yolo-jail''${PYTHONPATH:+:$PYTHONPATH}"
            exec ${pkgs.uv}/bin/uv run \
              --no-project \
              --with typer --with rich --with "pyjson5>=2.0.0" \
              -- ${pkgs.python313}/bin/python3 -c "from src.cli import main; main()" "$@"
          fi
          echo "YOLO Jail CLI: source not mounted at /opt/yolo-jail"
          echo "The yolo-jail repo is normally mounted automatically."
          exit 1
        '';

        # The Docker Image
        dockerImage = pkgs.dockerTools.buildLayeredImage {
          name = "yolo-jail";
          tag = "latest";
          created = "now";
          maxLayers = 100; # Optimize for faster loading by merging layers
          
          contents = [
            binPathLinks
            shims
            entrypoint
            yoloCli
            pkgs.bashInteractive
            pkgs.coreutils-full
            pkgs.git
            pkgs.ripgrep
            pkgs.fd
            pkgs.curl
            pkgs.cacert
            pkgs.mise
            pkgs.findutils
            pkgs.which
            pkgs.nodejs_22
            pkgs.python3
            pkgs.gh
            pkgs.gnused
            pkgs.gnugrep
            pkgs.gawk
            pkgs.gnupatch
            pkgs.diffutils
            pkgs.gzip
            pkgs.bzip2
            pkgs.xz
            pkgs.gnutar
            pkgs.unzip
            pkgs.zip
            pkgs.openssh
            pkgs.strace
            pkgs.lsof
            pkgs.file
            pkgs.gcc
            pkgs.gnumake
            pkgs.binutils
            pkgs.zlib
            pkgs.chromium   # For both MCP and Playwright
            pkgs.fontconfig
            pkgs.noto-fonts-color-emoji  # Emoji font for Chromium rendering
            pkgs.glibc.bin  # For ldd
            pkgs.procps     # ps, pgrep, pkill
            pkgs.net-tools  # netstat
            pkgs.iproute2   # ss, ip
            pkgs.iputils    # ping
            pkgs.dnsutils   # dig, host, nslookup
            pkgs.htop
            pkgs.neovim
            pkgs.hivemind
            pkgs.overmind
            pkgs.tmux
            pkgs.jq
            pkgs.bat
            pkgs.eza
            pkgs.delta
            pkgs.fzf
            pkgs.uv
            pkgs.nix          # For building nix images inside jail
            pkgs.podman       # For nested container support
            pkgs.fuse-overlayfs  # Storage driver for rootless podman
            pkgs.slirp4netns  # Rootless networking for nested podman
            pkgs.shadow       # newuidmap/newgidmap for user namespace mapping
          ] ++ extraPackages;

          # Create directories needed by nested podman and general operation
          fakeRootCommands = ''
            mkdir -p ./var/tmp ./run ./var/lib/containers

            # Podman needs /etc/passwd and /etc/group
            echo 'root:x:0:0:root:/root:/bin/bash' > ./etc/passwd
            echo 'root:x:0:' > ./etc/group
            echo 'nixbld:x:30000:' >> ./etc/group
          '';

          config = {
            Cmd = [ "/bin/bash" ];
            # We explicitly place shims first in PATH
            Env = [ 
              "PATH=${shims}/bin:/bin:/usr/bin" 
              "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              "LD_LIBRARY_PATH=/lib:/usr/lib"
              "FONTCONFIG_FILE=/etc/fonts/fonts.conf"
              "FONTCONFIG_PATH=/etc/fonts"
            ];
            WorkingDir = "/workspace";
          };
        };

      in
      {
        packages.default = dockerImage;
        packages.dockerImage = dockerImage;

        devShells.default = pkgs.mkShell {
          buildInputs = [
            pkgs.just
          ];
        };
      }
    );
}
