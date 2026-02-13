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
          
          # Link the dynamic linker for x86_64
          ln -s ${pkgs.stdenv.cc.bintools.dynamicLinker} $out/lib64/ld-linux-x86-64.so.2
          
          # Link standard libraries to both /lib and /usr/lib
          for dir in $out/lib $out/usr/lib; do
            ln -s ${pkgs.glibc}/lib/* $dir/
            ln -s ${pkgs.stdenv.cc.cc.lib}/lib/libstdc++.so.6 $dir/libstdc++.so.6
            ln -s ${pkgs.zlib}/lib/libz.so.1 $dir/libz.so.1
          done
        '';

        # Derivation for the entrypoint
        entrypoint = pkgs.writeShellScriptBin "yolo-entrypoint" (builtins.readFile ./src/entrypoint.sh);

        # The Docker Image
        dockerImage = pkgs.dockerTools.buildLayeredImage {
          name = "yolo-jail";
          tag = "latest";
          created = "now";
          
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
            pkgs.nodejs_22
            pkgs.python3
            pkgs.gh
            pkgs.gnused
            pkgs.gnugrep
            pkgs.gawk
            pkgs.findutils
            pkgs.gcc
            pkgs.gnumake
            pkgs.binutils
            pkgs.zlib
            pkgs.chromium   # For chrome-devtools-mcp
          ];

          config = {
            Cmd = [ "/bin/bash" ];
            # We explicitly place shims first in PATH
            Env = [ 
              "PATH=${shims}/bin:/bin:/usr/bin" 
              "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
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
