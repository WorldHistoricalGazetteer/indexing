# Force XeLaTeX
$pdf_mode = 5;
$postscript_mode = $dvi_mode = 0;

# Explicit command definitions
$xelatex = 'xelatex -file-line-error -interaction=nonstopmode -output-directory=pdf %O %S';

# Ensure bibtex looks in the output directory
$bibtex = 'bibtex %O %B';
$out_dir = 'pdf';

# This helps find files when using an output directory
$ENV{'TEXINPUTS'} = ".:./pdf:" . $ENV{'TEXINPUTS'};
$ENV{'BIBINPUTS'} = ".:./pdf:" . $ENV{'BIBINPUTS'};