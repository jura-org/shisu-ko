{
  description = "Shisu-ko: live Whisper subtitles for YouTube, minable into Anki";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f system);

      mkPkgs = system: cuda:
        let
          cpuPkgs = import nixpkgs { inherit system; config.allowUnfree = true; };
        in
        import nixpkgs {
          inherit system;
          config = {
            allowUnfree = true;
            cudaSupport = cuda;
          };
          # onnxruntime only runs the small VAD model; its CUDA build is huge and not cached,
          # so use the plain CPU build from the cache.
          overlays = nixpkgs.lib.optional cuda (final: prev: {
            pythonPackagesExtensions = prev.pythonPackagesExtensions ++ [
              (pyFinal: pyPrev: { onnxruntime = cpuPkgs.python3Packages.onnxruntime; })
            ];
          });
        };

      mkOutputs = pkgs: rec {
        python = pkgs.python3.withPackages (ps: with ps; [
          faster-whisper
          yt-dlp
          av
          numpy
          pytest
        ]);

        # yt-dlp needs a JS runtime for YouTube; nvidia-smi picks the compute type from free VRAM.
        runtimeTools = [ pkgs.deno pkgs.nodejs ];

        server = pkgs.writeShellApplication {
          name = "shisu-ko-server";
          runtimeInputs = runtimeTools;
          text = ''
            exec ${python}/bin/python ${self}/server/server.py "$@"
          '';
        };

        # The phone portal: yt-dlp for the video, PyAV for the card's picture; no Whisper.
        portalPython = pkgs.python3.withPackages (ps: with ps; [ yt-dlp av pytest ]);
        portal = pkgs.writeShellApplication {
          name = "shisu-ko-portal";
          runtimeInputs = runtimeTools;
          text = ''
            exec ${portalPython}/bin/python ${self}/portal/portal.py "$@"
          '';
        };

        # Restarts after a crash (driver reset, OOM); exit code 2 is a startup error, do not retry.
        serverLoop = pkgs.writeShellApplication {
          name = "shisu-ko";
          text = ''
            while true; do
              ${server}/bin/shisu-ko-server "$@" && exit 0
              code=$?
              [ "$code" -eq 2 ] && exit 2
              echo "The server stopped unexpectedly (exit code $code). Restarting in 5 seconds... press Ctrl+C to quit."
              sleep 5
            done
          '';
        };

        addon = pkgs.stdenvNoCC.mkDerivation {
          pname = "shisu-ko-addon";
          version = (builtins.fromJSON (builtins.readFile ./addon/manifest.json)).version;
          src = ./.;
          nativeBuildInputs = [ pkgs.nodejs ];
          buildPhase = ''
            node scripts/build.mjs --browser firefox
          '';
          installPhase = ''
            mkdir -p $out
            cp dist/shisu-ko-*-firefox.zip $out/
          '';
        };

        addon-chrome = pkgs.stdenvNoCC.mkDerivation {
          pname = "shisu-ko-addon-chrome";
          version = (builtins.fromJSON (builtins.readFile ./addon/manifest.json)).version;
          src = ./.;
          nativeBuildInputs = [ pkgs.nodejs ];
          buildPhase = ''
            node scripts/build.mjs --browser chrome
          '';
          installPhase = ''
            mkdir -p $out
            cp dist/shisu-ko-*-chrome.zip $out/
          '';
        };
      };
    in
    {
      packages = forAll (system:
        let
          cuda = mkOutputs (mkPkgs system true);
          cpu = mkOutputs (mkPkgs system false);
        in {
          default = cuda.serverLoop;
          server = cuda.server;
          server-cpu = cpu.server;
          python = cuda.python;
          addon = cuda.addon;
          addon-chrome = cuda.addon-chrome;
          portal = cuda.portal;
        });

      apps = forAll (system:
        let
          pkgs = mkPkgs system true;
          cuda = mkOutputs pkgs;
          app = drv: name: { type = "app"; program = "${drv}/bin/${name}"; };
          script = name: text: app (pkgs.writeShellApplication { inherit name text; }) name;
        in {
          default = app cuda.serverLoop "shisu-ko";
          server = app cuda.server "shisu-ko-server";
          portal = app cuda.portal "shisu-ko-portal";
          check = script "shisu-ko-check" ''
            ${cuda.server}/bin/shisu-ko-server --check
          '';
          tests = script "shisu-ko-tests" ''
            cd "''${SHISUKO_SRC:-${self}}"
            ${cuda.python}/bin/python -m pytest server/tests "$@"
            ${cuda.portalPython}/bin/python -m pytest portal/tests
            ${pkgs.nodejs}/bin/node --test addon/tests/*.test.js portal/tests/*.test.js
          '';
        });

      devShells = forAll (system:
        let
          pkgs = mkPkgs system true;
          cuda = mkOutputs pkgs;
        in {
          default = pkgs.mkShell {
            packages = [ cuda.python pkgs.web-ext ] ++ cuda.runtimeTools;
            shellHook = ''
              echo "shisu-ko dev shell"
              echo "  server:  python server/server.py [--model kotoba-tech/kotoba-whisper-v2.0-faster]"
              echo "  tests:   python -m pytest server/tests && node --test addon/tests/*.test.js"
              echo "  addon:   node scripts/build.mjs; npx web-ext lint --source-dir addon; npm run test:browser"
            '';
          };
        });
    };
}
