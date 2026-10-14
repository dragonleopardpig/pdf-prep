{
  description = "Whiten, speed up and OCR scanned PDFs, doing only what each scan needs";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: rec {
        pdf-prep = let
          scripts = pkgs.runCommand "pdf-prep-scripts" {} ''
            mkdir -p "$out"
            cp ${./pdf_prep.py} "$out/pdf_prep.py"
            cp ${./pdf_prep_edges.py} "$out/pdf_prep_edges.py"
            cp ${./pdf_prep_regions.py} "$out/pdf_prep_regions.py"
          '';
        in pkgs.writeShellApplication {
          name = "pdf-prep";
          runtimeInputs = [
            (pkgs.python3.withPackages (ps: with ps; [ pymupdf numpy pillow pikepdf ]))
            # Invoked as commands, not imported. Only the OCR pass needs them.
            pkgs.ocrmypdf
            pkgs.tesseract
          ];
          text = ''
            exec python3 ${scripts}/pdf_prep.py "$@"
          '';
        };
        default = pdf-prep;
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: with ps; [ pymupdf numpy pillow pikepdf ]))
            pkgs.ocrmypdf
            pkgs.tesseract
          ];
        };
      });
    };
}
