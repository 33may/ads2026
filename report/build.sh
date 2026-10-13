#!/bin/sh
# Render report.tex -> build/report.pdf
cd "$(dirname "$0")"
mkdir -p build
pdflatex -interaction=nonstopmode -halt-on-error -output-directory build report.tex >/dev/null || { grep -A3 '^!' build/report.log | head -20; exit 1; }
pdflatex -interaction=nonstopmode -halt-on-error -output-directory build report.tex >/dev/null
pdfinfo build/report.pdf | grep Pages
# refresh Preview (it does not reliably reload files rewritten by pdflatex)
osascript -e 'tell application "Preview" to close (every document whose name is "report.pdf")' >/dev/null 2>&1
open -g -a Preview build/report.pdf
