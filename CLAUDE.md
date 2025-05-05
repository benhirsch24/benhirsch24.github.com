# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build and Run Commands
- Generate site: `python site_builder.py`
- Convert markdown to HTML: `python md_to_html.py`
- Create a new post: `python create_post.py "Post Title"`
- Update index page: `python update_index.py`
- Create archive page: `python create_archive.py`

## Code Style Guidelines
- Use Python 3.x syntax with PEP 8 conventions
- Use 4 spaces for indentation (no tabs)
- Variable names: snake_case for variables and functions
- Constants: UPPER_CASE
- Error handling: Use try/except blocks with specific exception types
- String formatting: Use f-strings for string interpolation
- Documentation: Use docstrings for functions and modules
- Import organization: standard library first, then third-party, then local
- Required libraries: markdown, yaml, BeautifulSoup, slugify

## File Organization
- `posts_md/`: Markdown source for blog posts
- `posts/`: Generated HTML blog posts
- Post filenames: `YYYY-MM-DD_slug.md` → `YYYY-MM-DD-slug.html`