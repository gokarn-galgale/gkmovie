name: Update M3U Playlist

on:
  schedule:
    # Runs at 00:00 UTC every day
    - cron: '0 0 * * *'
  workflow_dispatch: # Allows manual trigger from Actions tab
  push:
    branches:
      - main

permissions:
  contents: write

jobs:
  build:
    runs-on: ubuntu-latest

    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install Dependencies
        run: |
          python -m pip install --upgrade pip
          pip install requests beautifulsoup4

      - name: Generate M3U Playlist
        run: |
          python generate_playlist.py

      - name: Commit and Push Updated Playlist
        run: |
          git config --global user.name "github-actions[bot]"
          git config --global user.email "github-actions[bot]@users.noreply.github.com"
          git add playlist.m3u
          if git diff --staged --quiet; then
            echo "No changes found in playlist."
          else
            git commit -m "Auto-update playlist.m3u [skip ci]"
            git push
          fi
