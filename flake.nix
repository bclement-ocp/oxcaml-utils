{
  description = "Develop Python on Nix with uv";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs =
    { nixpkgs, ... }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs lib.systems.flakeExposed;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python3Packages = pkgs.python3Packages;
        in
        {
          default =
            python3Packages.buildPythonApplication {
              name = "ox";
              pyproject = true;

              src = ./.;

              build-system = with python3Packages; [ setuptools ];

              dependencies = with python3Packages; [
                patiencediff
                polars
              ];

              makeWrapperArgs = [
                "--prefix PATH : ${lib.makeBinPath [ pkgs.nix ]}"
              ];
            };
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.python3
              pkgs.uv
            ];

            # env = lib.optionalAttrs pkgs.stdenv.isLinux {
            #   # Python libraries often load native shared objects using dlopen(3).
            #   # Setting LD_LIBRARY_PATH makes the dynamic library loader aware of libraries without using RPATH for lookup.
            #   LD_LIBRARY_PATH = lib.makeLibraryPath pkgs.pythonManylinuxPackages.manylinux1;
            # };

            shellHook = ''
              unset PYTHONPATH
              uv sync
              . .venv/bin/activate
            '';

            nativeBuildInputs = with pkgs; [
              stdenv.cc.cc.lib
            ];

            LD_LIBRARY_PATH = "$LD_LIBRARY_PATH:${pkgs.stdenv.cc.cc.lib}/lib";
          };
        }
      );
    };
}
