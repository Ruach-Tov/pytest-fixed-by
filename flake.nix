# Usage in NixOS configuration.nix:
#   1. Add to flake inputs:
#        pytest-fixed-by.url = "github:Ruach-Tov/pytest-fixed-by";
#   2. In your Python environment:
#        (python3.withPackages (ps: [
#          inputs.pytest-fixed-by.packages.${system}.default
#          ps.pytest
#        ]))
{
  description = "pytest-fixed-by — Prove your regression test catches the regression";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        pytest-fixed-by = pkgs.python3Packages.buildPythonPackage {
          pname = "pytest-fixed-by";
          version = "0.1.0";
          format = "pyproject";

          src = ./.;

          nativeBuildInputs = with pkgs.python3Packages; [ hatchling ];

          propagatedBuildInputs = with pkgs.python3Packages; [ pytest ];

          nativeCheckInputs = with pkgs.python3Packages; [ pytestCheckHook ]
            ++ [ pkgs.git ];

          pythonImportsCheck = [ "pytest_fixed_by" ];

          meta = with pkgs.lib; {
            description = "Prove your regression test catches the regression";
            longDescription = ''
              A pytest decorator and verification protocol that mechanically
              proves a test catches the specific bug it claims to cover.
              Uses git worktrees to run today's test against yesterday's code.
            '';
            homepage = "https://github.com/Ruach-Tov/pytest-fixed-by";
            license = licenses.mit;
            maintainers = [ ];
          };
        };
      in
      {
        packages = {
          default = pytest-fixed-by;
          pytest-fixed-by = pytest-fixed-by;
        };

        devShells.default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [
              pytest-fixed-by
              ps.pytest
            ]))
            pkgs.git
          ];
        };
      }
    );
}
