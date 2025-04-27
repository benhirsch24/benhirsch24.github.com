#!/usr/bin/env python3
import re
import os
from html import unescape

def extract_title(html_content):
    title_match = re.search(r'<h2 class="post-title">(.*?)</h2>', html_content, re.DOTALL)
    if title_match:
        return unescape(title_match.group(1))

    # Fallback to looking in the title tag
    title_tag_match = re.search(r'<title>(.*?) - Ben\'s Blog</title>', html_content, re.DOTALL)
    if title_tag_match:
        return unescape(title_tag_match.group(1))

    return "Untitled Post"

def extract_date_from_filename(filename):
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
    if date_match:
        return date_match.group(1)
    return "Unknown Date"

def convert_html_to_markdown(html_content):
    # Extract the post content
    body_match = re.search(r'<section class="post-body">(.*?)</section>', html_content, re.DOTALL)
    if not body_match:
        return "Error: Could not find post body"

    content = body_match.group(1)

    # Convert HTML to Markdown

    # Remove the <!--more--> tag
    content = re.sub(r'<!--more-->', '', content)

    # Convert headers
    for i in range(6, 0, -1):
        pattern = rf'<h{i}[^>]*>(.*?)</h{i}>'
        repl = lambda m: '#' * i + ' ' + m.group(1).strip()
        content = re.sub(pattern, repl, content, flags=re.DOTALL)

    # Convert inline code blocks
    content = re.sub(r'<code>(.*?)</code>', r'`\1`', content, flags=re.DOTALL)

    # Convert code blocks
    content = re.sub(r'<pre><code>(.*?)</code></pre>', lambda m: '```\n' + m.group(1) + '\n```', content, flags=re.DOTALL)

    # Convert links
    content = re.sub(r'<a href="([^"]*)"[^>]*>(.*?)</a>', r'[\2](\1)', content, flags=re.DOTALL)

    # Convert emphasis
    content = re.sub(r'<em>(.*?)</em>', r'*\1*', content, flags=re.DOTALL)
    content = re.sub(r'<strong>(.*?)</strong>', r'**\1**', content, flags=re.DOTALL)

    # Convert paragraphs
    content = re.sub(r'<p>(.*?)</p>', r'\n\1\n', content, flags=re.DOTALL)

    # Clean up any remaining HTML tags
    content = re.sub(r'<[^>]*>', '', content)

    # Unescape HTML entities
    content = unescape(content)

    # Remove extra newlines
    content = re.sub(r'\n{3,}', '\n\n', content)

    return content.strip()

def process_html_file(html_content, filename, output_dir):
    print(f"Processing {filename}")
    title = extract_title(html_content)
    print(f"Extracted title {title}")
    date = extract_date_from_filename(filename)
    print(f"Extracted date {date}")
    markdown_content = convert_html_to_markdown(html_content)
    print(f"Extracted markdown content {markdown_content}")

    # Create YAML frontmatter
    yaml_frontmatter = f"""---
title: "{title}"
date: {date}
---

"""

    # Create markdown file path
    md_filename = os.path.splitext(filename)[0] + '.md'
    md_path = os.path.join(output_dir, md_filename)

    # Write markdown file
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(yaml_frontmatter + markdown_content)

    return md_path

# Hardcoded list of files
files = [
    "/Users/ben/benhirsch24.github.com/posts/2013-05-12-cuda-in-terms-of-list-operations.html",
#    "/Users/ben/benhirsch24.github.com/posts/2013-05-20-different-programming-styles-this-quarter.html",
#    "/Users/ben/benhirsch24.github.com/posts/2013-06-19-fun-with-elm-531-calculator.html",
#    "/Users/ben/benhirsch24.github.com/posts/2013-06-26-go-in-elm.html",
#    "/Users/ben/benhirsch24.github.com/posts/2013-07-03-c-interactive-running.html",
#    "/Users/ben/benhirsch24.github.com/posts/2013-08-09-benchmarking-cuda-vs-eigen.html"
]

# To be filled with content by Claude
html_contents = [
    # Content will be filled here
]

output_dir = "/Users/ben/benhirsch24.github.com/posts_md"
os.makedirs(output_dir, exist_ok=True)

for i, file_path in enumerate(files):
    try:
        basename = os.path.basename(file_path)
        md_path = process_html_file(html_contents[i], basename, output_dir)
        print(f"Converted {file_path} to {md_path}")
    except Exception as e:
        print(f"Error processing {file_path}: {str(e)}")
