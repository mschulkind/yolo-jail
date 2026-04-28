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

        # Docker images are always Linux containers.  When building on macOS
        # (darwin), map to the equivalent Linux system so the image gets native
        # Linux packages (e.g. aarch64-darwin → aarch64-linux).
        # Requires a Linux builder in Nix (nix-darwin linux-builder, remote
        # builder, or Docker-based builder).
        imageSystem = builtins.replaceStrings ["-darwin"] ["-linux"] system;
        imagePkgs = nixpkgs.legacyPackages.${imageSystem};

        # Architecture-aware multilib path for LD_LIBRARY_PATH inside the image
        linuxMultilib =
          if imageSystem == "x86_64-linux" then "x86_64-linux-gnu"
          else if imageSystem == "aarch64-linux" then "aarch64-linux-gnu"
          else "${builtins.head (builtins.split "-" imageSystem)}-linux-gnu";

        # Extra packages from project config (passed via YOLO_EXTRA_PACKAGES env var).
        # Three formats:
        #   "strace"                                          → latest from flake nixpkgs
        #   {"name": "freetype", "nixpkgs": "<commit>"}      → pinned nixpkgs commit
        #   {"name": "freetype", "version": "2.14.1",        → version override (build from source)
        #    "url": "mirror://savannah/freetype/freetype-2.14.1.tar.xz",
        #    "hash": "sha256-..."}
        extraPackageSpecs = let
          raw = builtins.getEnv "YOLO_EXTRA_PACKAGES";
        in
          if raw == "" then [] else builtins.fromJSON raw;

        extraPackages = map (spec:
          if builtins.isString spec then
            imagePkgs.${spec}
          else if spec ? nixpkgs then
            # Pinned to a specific nixpkgs commit
            let
              pinnedPkgs = import (builtins.fetchTarball {
                url = "https://github.com/NixOS/nixpkgs/archive/${spec.nixpkgs}.tar.gz";
              }) { system = imageSystem; };
            in pinnedPkgs.${spec.name}
          else if spec ? version && spec ? url && spec ? hash then
            # Version override: rebuild existing package with different source
            imagePkgs.${spec.name}.overrideAttrs (old: {
              version = spec.version;
              src = imagePkgs.fetchurl {
                url = spec.url;
                hash = spec.hash;
              };
            })
          else
            imagePkgs.${spec.name}
        ) extraPackageSpecs;

        # Derivation for the shim scripts (plain text — built on host, runs in container)
        shims = imagePkgs.stdenv.mkDerivation {
          name = "yolo-shims";
          src = ./src/shims;
          installPhase = ''
            mkdir -p $out/bin
            cp * $out/bin/
            chmod +x $out/bin/*
          '';
        };

        # Derivation to provide /usr/bin/env and other standard paths.
        # `withChromium` controls whether chromium shims + font links are
        # created.  `withNestedPodman` controls rootless-podman config files
        # under /etc/containers.  Both are opt-out so the minimal image
        # variant can skip the bulky and/or unused plumbing.
        mkBinPathLinks = { withChromium ? true, withNestedPodman ? true }:
          imagePkgs.runCommand "bin-path-links" {} (''
          mkdir -p $out/usr/bin $out/bin $out/lib64 $out/lib $out/usr/lib $out/etc $out/usr/share/fonts $out/usr/share
          ln -s ${imagePkgs.coreutils}/bin/env $out/usr/bin/env
          ln -s ${imagePkgs.bashInteractive}/bin/bash $out/bin/bash
          ln -s ${imagePkgs.bashInteractive}/bin/sh $out/bin/sh
          ln -s ${imagePkgs.gawk}/bin/awk $out/bin/awk
          ln -s ${imagePkgs.gnused}/bin/sed $out/bin/sed
          ln -s ${imagePkgs.gnugrep}/bin/grep $out/bin/grep
          ln -s ${imagePkgs.findutils}/bin/find $out/bin/find
          # /usr/share/zoneinfo → tzdata store path.  glibc already
          # reads TZDIR, but Python's ``zoneinfo`` module + other
          # clients search the standard FHS path first, so symlink
          # it so both paths work.
          ln -s ${imagePkgs.tzdata}/share/zoneinfo $out/usr/share/zoneinfo
        '' + imagePkgs.lib.optionalString withChromium ''
          ln -s ${imagePkgs.chromium}/bin/chromium $out/usr/bin/chromium
          ln -s ${imagePkgs.chromium}/bin/chromium $out/usr/bin/google-chrome
          ln -s ${imagePkgs.chromium}/bin/chromium $out/usr/bin/chrome
          ln -s ${imagePkgs.fontconfig.out}/etc/fonts $out/etc/fonts
        '' + ''

          # Link the dynamic linker at conventional paths (architecture-aware)
          LINKER_BASENAME=$(basename "${imagePkgs.stdenv.cc.bintools.dynamicLinker}")
          ln -sf ${imagePkgs.stdenv.cc.bintools.dynamicLinker} $out/lib/$LINKER_BASENAME
          ln -sf ${imagePkgs.stdenv.cc.bintools.dynamicLinker} $out/lib64/$LINKER_BASENAME

          # Link shared libraries to /lib and /usr/lib for LD_LIBRARY_PATH discovery.
          # Iterates over all packages with lib outputs, including split-output packages
          # (e.g., fontconfig.lib has .so files separate from fontconfig.out which has etc/).
          # Note: glib and pango define outputs=["bin" "out" ...] so their DEFAULT output
          # is "bin" (no lib/). Must use .out explicitly to get the libraries.
          # Non-nix binaries (node, npm/pip packages) rely on LD_LIBRARY_PATH=/lib:/usr/lib
          # since they lack RPATH entries pointing into the nix store.
          for dir in $out/lib $out/usr/lib; do
            for pkg in ${imagePkgs.glibc} \
                       ${imagePkgs.stdenv.cc.cc.lib} \
                       ${imagePkgs.zlib}; do
              if [ -d "$pkg/lib" ]; then
                for f in "$pkg"/lib/lib*.so*; do
                  [ -f "$f" ] || [ -L "$f" ] || continue
                  name=$(basename "$f")
                  [ ! -e "$dir/$name" ] && ln -s "$f" "$dir/$name" 2>/dev/null || true
                done
              fi
            done
          done
        '' + imagePkgs.lib.optionalString withChromium ''
          # Chromium graphics stack — only linked when chromium itself is in
          # the image.  The minimal variant has neither the binary nor the libs.
          for dir in $out/lib $out/usr/lib; do
            for pkg in ${imagePkgs.fontconfig.lib} \
                       ${imagePkgs.glib.out} \
                       ${imagePkgs.pango.out} \
                       ${imagePkgs.cairo} \
                       ${imagePkgs.harfbuzz} \
                       ${imagePkgs.freetype} \
                       ${imagePkgs.fribidi} \
                       ${imagePkgs.pixman} \
                       ${imagePkgs.libpng} \
                       ${imagePkgs.expat} \
                       ${imagePkgs.pcre2} \
                       ${imagePkgs.libffi}; do
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
          for fontPkg in ${imagePkgs.noto-fonts-color-emoji}; do
            if [ -d "$fontPkg/share/fonts" ]; then
              for d in "$fontPkg"/share/fonts/*; do
                [ -d "$d" ] && ln -s "$d" "$out/usr/share/fonts/$(basename "$d")" 2>/dev/null || true
              done
            fi
          done
        '' + imagePkgs.lib.optionalString withNestedPodman ''
          # Podman nested container support
          echo "root:100000:65536" > $out/etc/subuid
          echo "root:100000:65536" > $out/etc/subgid

          # Podman storage config for rootless operation
          mkdir -p $out/etc/containers
          cat > $out/etc/containers/storage.conf <<STORAGE
          [storage]
          driver = "overlay"
          [storage.options.overlay]
          mount_program = "${imagePkgs.fuse-overlayfs}/bin/fuse-overlayfs"
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
        '' + ''

          # /etc/ld.so.cache so non-nix binaries that don't read
          # LD_LIBRARY_PATH (setuid helpers, some dlopen callers) can
          # still find libstdc++, glibc, etc.  The symlinks created
          # above under /lib and /usr/lib are the scan roots.
          #
          # ``-r $out`` tells ldconfig to treat $out as root while it
          # scans, so the paths it records in the cache are absolute
          # paths that will resolve at runtime inside the container.
          # Failure shouldn't abort the image build — if ldconfig
          # can't index something we fall back to LD_LIBRARY_PATH
          # which is already wired for every standard path.
          {
            echo "/lib"
            echo "/usr/lib"
            echo "/usr/lib/${linuxMultilib}"
          } > $out/etc/ld.so.conf
          ${imagePkgs.glibc.bin}/bin/ldconfig \
            -r $out -C /etc/ld.so.cache -f /etc/ld.so.conf || true
        '');

        binPathLinks = mkBinPathLinks { };
        binPathLinksMinimal = mkBinPathLinks {
          withChromium = false;
          withNestedPodman = false;
        };

        # Derivation for the Python entrypoint (runs inside Linux container)
        entrypointScript = imagePkgs.writeTextFile {
          name = "yolo-entrypoint-py";
          text = builtins.readFile ./src/entrypoint.py;
          destination = "/lib/yolo-entrypoint.py";
        };
        entrypoint = imagePkgs.writeShellScriptBin "yolo-entrypoint" ''
          exec ${imagePkgs.python313}/bin/python3 ${entrypointScript}/lib/yolo-entrypoint.py "$@"
        '';

        # In-jail yolo CLI wrapper — delegates to the mounted repo via uv
        yoloCli = imagePkgs.writeShellScriptBin "yolo" ''
          # Use the mounted repo with uv (deps are cached in persistent ~/.cache/uv)
          if [ -d /opt/yolo-jail/src ]; then
            export PYTHONPATH="/opt/yolo-jail''${PYTHONPATH:+:$PYTHONPATH}"
            exec ${imagePkgs.uv}/bin/uv run \
              --no-project \
              --python ${imagePkgs.python313}/bin/python3 \
              --with typer --with rich --with "pyjson5>=2.0.0" \
              -- python3 -c "from src.cli import main; main()" "$@"
          fi
          echo "YOLO Jail CLI: source not mounted at /opt/yolo-jail"
          echo "The yolo-jail repo is normally mounted automatically."
          exit 1
        '';

        # Core packages: everything the integration test suite in
        # tests/test_jail.py actually touches, plus POSIX essentials that
        # shell scripts in src/entrypoint.py and src/shims/ rely on.
        # Shared between the full and minimal image variants.
        corePackages = [
          shims
          entrypoint
          yoloCli
          imagePkgs.bashInteractive
          imagePkgs.coreutils-full
          imagePkgs.git
          imagePkgs.ripgrep
          imagePkgs.fd
          imagePkgs.curl          # real curl for host-port-forwarding tests
          imagePkgs.cacert
          imagePkgs.mise
          imagePkgs.findutils
          imagePkgs.which
          imagePkgs.nodejs_22
          imagePkgs.python3
          imagePkgs.gh
          imagePkgs.gnused
          imagePkgs.gnugrep
          imagePkgs.gawk
          imagePkgs.gnupatch
          imagePkgs.diffutils
          imagePkgs.gzip
          imagePkgs.bzip2
          imagePkgs.xz
          imagePkgs.gnutar
          imagePkgs.unzip
          imagePkgs.zip
          imagePkgs.zlib
          imagePkgs.procps        # ps, pgrep, pkill
          imagePkgs.overmind      # exercised by overmind isolation tests
          imagePkgs.jq
          imagePkgs.uv
          imagePkgs.iptables      # DNAT rules (published port → localhost fixup)
          imagePkgs.socat         # host port forwarding into the jail
          imagePkgs.sox           # Claude Code's `/voice` recorder depends on it
          # Timezone database — without it, glibc can't resolve
          # ``TZ=America/New_York`` etc. and silently falls back to UTC,
          # so `date` inside the jail reports wall-clock time that
          # disagrees with the host.  TZDIR in the image env below
          # points glibc at this store path.
          imagePkgs.tzdata
        ];

        # Extras that bulk the image up but aren't exercised by the
        # integration test suite.  Kept out of the minimal variant so CI
        # integration runs don't need to load ~2 GB of unused bytes.
        fullPackages = [
          imagePkgs.openssh
          imagePkgs.strace
          imagePkgs.lsof
          imagePkgs.file
          imagePkgs.gcc
          imagePkgs.gnumake
          imagePkgs.binutils
          imagePkgs.chromium                   # For both MCP and Playwright
          imagePkgs.fontconfig
          imagePkgs.noto-fonts-color-emoji     # Emoji font for Chromium rendering
          imagePkgs.glibc.bin                  # ldd
          imagePkgs.net-tools                  # netstat
          imagePkgs.iproute2                   # ss, ip
          imagePkgs.iputils                    # ping
          imagePkgs.dnsutils                   # dig, host, nslookup
          imagePkgs.htop
          imagePkgs.hivemind
          imagePkgs.tmux
          imagePkgs.bat
          imagePkgs.eza
          imagePkgs.delta
          imagePkgs.fzf
          imagePkgs.nix                        # nested nix builds inside jail
          imagePkgs.podman                     # nested container support
          imagePkgs.fuse-overlayfs             # storage driver for rootless podman
          imagePkgs.slirp4netns                # rootless networking for nested podman
          imagePkgs.shadow                     # newuidmap/newgidmap
        ];

        mkDockerImage = { minimal ? false }:
          imagePkgs.dockerTools.streamLayeredImage {
            name = "yolo-jail";
            tag = if minimal then "ci-minimal" else "latest";
            created = "now";
            maxLayers = 100;

            contents =
              [ (if minimal then binPathLinksMinimal else binPathLinks) ]
              ++ corePackages
              ++ (if minimal then [] else fullPackages)
              ++ extraPackages;

            # Create directories needed by nested podman and general operation
            fakeRootCommands = ''
              mkdir -p ./var/tmp ./var/cache ./var/log ./run ./var/lib/containers

              # Pre-create mountpoint directories for --read-only root filesystem.
              # With --read-only, the OCI runtime cannot create these on the fly.
              mkdir -p ./home/agent ./workspace ./tmp ./opt/yolo-jail ./mise
              mkdir -p ./ctx/host-claude ./ctx/host-nvim-config
              mkdir -p ./nix/var/nix/daemon-socket

              # Podman needs /etc/passwd and /etc/group
              echo 'root:x:0:0:root:/home/agent:/bin/bash' > ./etc/passwd
              echo 'root:x:0:' > ./etc/group
              echo 'nixbld:x:30000:' >> ./etc/group
            '';

            config = {
              Cmd = [ "/bin/bash" ];
              # We explicitly place shims first in PATH
              Env = [
                "PATH=${shims}/bin:/bin:/usr/bin"
                "SSL_CERT_FILE=${imagePkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
                "LD_LIBRARY_PATH=/lib:/usr/lib:/usr/lib/${linuxMultilib}"
                "FONTCONFIG_FILE=/etc/fonts/fonts.conf"
                "FONTCONFIG_PATH=/etc/fonts"
                # Point glibc at the tzdata store path so ``TZ=<zone>``
                # passed from the host resolves (otherwise date + glibc
                # fall back to UTC, diverging from the host clock).
                "TZDIR=${imagePkgs.tzdata}/share/zoneinfo"
              ];
              WorkingDir = "/workspace";
            };
          };

        dockerImage = mkDockerImage { minimal = false; };
        dockerImageMinimal = mkDockerImage { minimal = true; };

      in
      {
        packages.default = dockerImage;
        packages.dockerImage = dockerImage;
        packages.dockerImageMinimal = dockerImageMinimal;

        devShells.default = pkgs.mkShell {
          buildInputs = [
            pkgs.just
          ];
        };
      }
    );
}
