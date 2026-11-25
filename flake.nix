{
  description = "aptfinder: local apartment finder (append-only listing store, goal filters, map dashboard)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      # claude-code is unfree; allow only it. It runs `extract`, `discover-sites` (headless `claude -p`).
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfreePredicate = pkg: builtins.elem (nixpkgs.lib.getName pkg) [ "claude-code" ];
      };
      py = pkgs.python312.pkgs;

      # apify-client is not in nixpkgs: the same wheels uv.lock pins.
      impit = py.buildPythonPackage {
        pname = "impit";
        version = "0.15.3";
        format = "wheel";
        src = pkgs.fetchurl {
          url = "https://files.pythonhosted.org/packages/dd/67/30871ae26f49d0e370bbeeffdd53f2aae31fd27d2485a8f5a260abcaa078/impit-0.15.3-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl";
          sha256 = "be41985aa1c0058d5fd0dcd8267cd63a68ec3df03eaeecaee25a56dceeeae8e9";
        };
        nativeBuildInputs = [ pkgs.autoPatchelfHook ];
        buildInputs = [ pkgs.stdenv.cc.cc.lib ];
      };
      apify-client = py.buildPythonPackage {
        pname = "apify-client";
        version = "3.5.1";
        format = "wheel";
        src = pkgs.fetchurl {
          url = "https://files.pythonhosted.org/packages/63/df/ca34d8118f31e0d874f69dceca4ccae6d55520cca676c5b0be779a00aa9b/apify_client-3.5.1-py3-none-any.whl";
          sha256 = "543b8d8afca8aef85a53eb406f51cb0a02e7fe7b8c3a850800c282c6312e587e";
        };
        dependencies = [ impit py.colorama py.more-itertools py.pydantic py.typing-extensions ];
      };

      aptfinder = py.buildPythonApplication {
        pname = "aptfinder";
        version = "0.1.0";
        pyproject = true;
        src = ./.;
        build-system = [ py.hatchling ];
        dependencies = [ py.pydantic py.httpx py.fastapi py.uvicorn py.python-dotenv py.pillow py.imagehash py.cairosvg apify-client ];
        # config/, data/ and .env are read from the directory you run in (override with APTF_ROOT).
        makeWrapperArgs = [ "--run" "export APTF_ROOT=\${APTF_ROOT:-$PWD}"
                            "--prefix" "PATH" ":" "${pkgs.claude-code}/bin"
                            "--prefix" "PATH" ":" "${pkgs.curl}/bin" ];  # curl for media curl_hosts  # claude for extract / discover-sites
        doCheck = false; # tests run with `uv run pytest` in the repo
        meta.license = pkgs.lib.licenses.mit;
      };

      # Dashboard plus Cloudflare tunnel, token from .env (CLOUDFLARED_TOKEN). The tunnel's public hostname
      # (set in the Cloudflare dashboard, service http://localhost:8765) must be behind a Cloudflare Access
      # policy: the dashboard has write endpoints and no login. Run from the repo: nix run .#tunnel [-- --redact]
      tunnel = pkgs.writeShellApplication {
        name = "aptf-tunnel";
        runtimeInputs = [ aptfinder pkgs.cloudflared ];
        text = ''
          # shellcheck source=/dev/null
          if [ -f .env ]; then set -a; . ./.env; set +a; fi
          : "''${CLOUDFLARED_TOKEN:?put CLOUDFLARED_TOKEN (the tunnel token only) in .env}"
          port="''${APTF_PORT:-8765}"
          aptf-web --port "$port" "$@" &
          web=$!
          trap 'kill "$web"' EXIT
          TUNNEL_TOKEN="$CLOUDFLARED_TOKEN" cloudflared tunnel --no-autoupdate run
        '';
      };

      app = bin: { type = "app"; program = "${aptfinder}/bin/${bin}"; };
    in
    {
      packages.${system} = {
        default = aptfinder;
        inherit aptfinder tunnel;
        cloudflared = pkgs.cloudflared;
      };
      # nix run .#<name> -- <args>: one app per aptf-* command, plus the tunnel.
      apps.${system} = {
        default = app "aptf-web";
        web = app "aptf-web";
        db = app "aptf-db";
        intake = app "aptf-intake";
        identity = app "aptf-identity";
        commute = app "aptf-commute";
        filter = app "aptf-filter";
        verify = app "aptf-verify";
        eval = app "aptf-eval";
        media = app "aptf-media";
        tunnel = { type = "app"; program = "${tunnel}/bin/aptf-tunnel"; };
      };
      devShells.${system}.default = pkgs.mkShell {
        packages = [ pkgs.uv pkgs.python312 pkgs.cloudflared pkgs.claude-code ];
        shellHook = "unset PYTHONPATH"; # a system PYTHONPATH (e.g. ROS) breaks pytest
      };
    };
}
