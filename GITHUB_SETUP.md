# Put Orchard on GitHub from your computer

This download is a source snapshot, not a Git checkout. The original ZIP had no Git history. The commands below clone the real repository first, preserve its history, and add this implementation on a new branch. Nothing has been pushed yet.

## macOS / Linux

Place the extracted `orchard` folder in a working directory. Open Terminal in the **parent directory** of that folder, then run:

```bash
git clone https://github.com/shawntz/orchard.git orchard-github
rsync -av --exclude='.git' --exclude='data/' --exclude='__pycache__/' --exclude='*.pyc' --exclude='.env' orchard/ orchard-github/
cd orchard-github
git switch -c orchard-v1
mkdir -p data
touch data/.gitkeep
python3 -m unittest discover -s tests -v
git add README.md GITHUB_SETUP.md .gitignore .env.example pyproject.toml .github orchard scripts tests docs data/.gitkeep
git diff --cached --stat
git commit -m "Build Orchard local literature search workspace"
git push -u origin orchard-v1
```

Use your normal GitHub sign-in flow if prompted. Do not put a personal access token in a command, source file, or chat. If Git asks for your commit identity, configure your own name and email on your computer before committing.

Open `https://github.com/shawntz/orchard` and create a pull request from `orchard-v1` to the repository's default branch. The test workflow is provided, but has not been run on GitHub yet. You can review the changed files before merging.

If that branch already exists, choose a new branch name rather than force-pushing. If you already have a checkout, copy the source into a clean new branch in that checkout instead of overwriting uncommitted work.

## Windows or graphical workflow

Clone `shawntz/orchard` with GitHub Desktop, create an `orchard-v1` branch, and copy the supplied source files into that checkout. Include `.github`, `.gitignore`, and `.env.example`. **Do not copy `data`, local exports, or Python cache directories into the commit.** Review the changed-file list, commit, and publish the branch.

## Your preloaded search terms

The download's `data` folder contains your original imported source and draft plans. It stays on your computer and is excluded from Git. A fresh Git clone starts with an empty database. Import your original `.txt` through the UI, or run:

```bash
python3 scripts/import_terms.py "/absolute/path/to/your-search-terms.txt" --eyeris-drafts
```

This command creates unapproved drafts only when the expected Eyeris headings exist; it does not run searches or invent terms. The provided local project can be run directly without pushing anything.
