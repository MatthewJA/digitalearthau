

for name in `find . -name '*.html'`; do
    path=${name#./}
    cat > $name <<EOF
<!DOCTYPE html>
<meta charset="utf-8">
<title>Redirecting to https://docs.dea.ga.gov.au/$path</title>
<meta http-equiv="refresh" content="0; URL=https://docs.dea.ga.gov.au/$path">
<link rel="canonical" href="https://docs.dea.ga.gov.au/$path">
EOF
done