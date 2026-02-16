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
          mkdir -p $out/usr/bin $out/bin $out/lib64 $out/lib $out/usr/lib
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
          
          # Link standard libraries to both /lib and /usr/lib
          for dir in $out/lib $out/usr/lib; do
            ln -s ${pkgs.glibc}/lib/* $dir/
            ln -s ${pkgs.stdenv.cc.cc.lib}/lib/libstdc++.so.6 $dir/libstdc++.so.6
            ln -s ${pkgs.zlib}/lib/libz.so.1 $dir/libz.so.1
            # GLVND dispatcher libraries (load vendor-specific GL/EGL at runtime)
            ln -s ${pkgs.libglvnd}/lib/libEGL.so* $dir/ 2>/dev/null || true
            ln -s ${pkgs.libglvnd}/lib/libGL.so* $dir/ 2>/dev/null || true
            ln -s ${pkgs.libglvnd}/lib/libGLESv2.so* $dir/ 2>/dev/null || true
            ln -s ${pkgs.libglvnd}/lib/libGLX.so* $dir/ 2>/dev/null || true
            ln -s ${pkgs.libglvnd}/lib/libGLdispatch.so* $dir/ 2>/dev/null || true
            ln -s ${pkgs.libglvnd}/lib/libOpenGL.so* $dir/ 2>/dev/null || true
            # Mesa vendor drivers (fallback when no NVIDIA GPU)
            ln -s ${pkgs.mesa}/lib/libEGL_mesa.so* $dir/ 2>/dev/null || true
            ln -s ${pkgs.mesa}/lib/libGLX_mesa.so* $dir/ 2>/dev/null || true
            ln -s ${pkgs.mesa}/lib/libgbm*.so* $dir/ 2>/dev/null || true
            ln -s ${pkgs.libdrm}/lib/libdrm*.so* $dir/ 2>/dev/null || true
            ln -s ${pkgs.vulkan-loader}/lib/libvulkan*.so* $dir/ 2>/dev/null || true
          done

          # EGL vendor config for NVIDIA (GLVND uses this to find libEGL_nvidia.so)
          mkdir -p $out/usr/share/glvnd/egl_vendor.d
          cat > $out/usr/share/glvnd/egl_vendor.d/10_nvidia.json <<EOF
          {
              "file_format_version" : "1.0.0",
              "ICD" : {
                  "library_path" : "libEGL_nvidia.so.0"
              }
          }
          EOF
          # Mesa EGL vendor (lower priority fallback)
          cat > $out/usr/share/glvnd/egl_vendor.d/50_mesa.json <<EOF
          {
              "file_format_version" : "1.0.0",
              "ICD" : {
                  "library_path" : "libEGL_mesa.so.0"
              }
          }
          EOF
        '';

        # Derivation for the entrypoint
        entrypoint = pkgs.writeShellScriptBin "yolo-entrypoint" (builtins.readFile ./src/entrypoint.sh);

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
            pkgs.freefont_ttf
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
            # GPU acceleration libraries
            pkgs.libglvnd   # GL Vendor-Neutral Dispatch (libEGL.so.1, libGL.so.1)
            pkgs.mesa
            pkgs.libdrm
            pkgs.vulkan-loader
          ] ++ extraPackages;

          config = {
            Cmd = [ "/bin/bash" ];
            # We explicitly place shims first in PATH
            Env = [ 
              "PATH=${shims}/bin:/bin:/usr/bin" 
              "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              "LD_LIBRARY_PATH=/lib:/usr/lib:/usr/lib64"
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
