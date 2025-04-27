#!/usr/bin/env python3

import markdown
import os

# Configuration
ABOUT_MD = "/Users/ben/benhirsch24.github.com/about.md"
ABOUT_HTML = "/Users/ben/benhirsch24.github.com/about.html"
LAYOUT_HTML = "/Users/ben/benhirsch24.github.com/layout.html"

def read_markdown_file(file_path):
    """Read markdown file content"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return None

def read_layout_template():
    """Read the layout template file"""
    try:
        with open(LAYOUT_HTML, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print(f"Error reading layout template: {e}")
        return None

def extract_title(content):
    """Extract title from the first heading"""
    lines = content.split('\n')
    for line in lines:
        if line.startswith('# '):
            return line[2:].strip()
    return "About Me"

def convert_markdown_to_html():
    """Convert about.md to HTML using the layout template"""
    # Read the markdown content
    md_content = read_markdown_file(ABOUT_MD)
    if not md_content:
        print("Error: Couldn't read about.md")
        return False
    
    # Extract title
    title = extract_title(md_content)
    
    # Convert markdown to HTML
    html_content = markdown.markdown(
        md_content,
        extensions=['markdown.extensions.fenced_code', 'markdown.extensions.tables']
    )
    
    # Wrap in a container
    wrapped_html = f"""
    <article class="about">
        <div class="about-content">
            {html_content}
        </div>
    </article>
    """
    
    # Read the layout template
    layout_template = read_layout_template()
    if not layout_template:
        print("Error: Couldn't read layout template")
        return False
    
    # Insert the content into the layout template
    about_html = layout_template.replace("{{ content }}", wrapped_html)
    about_html = about_html.replace("{{ page_title }}", title)
    
    # Write the updated HTML to about.html
    try:
        with open(ABOUT_HTML, 'w', encoding='utf-8') as f:
            f.write(about_html)
        print(f"Created {ABOUT_HTML}")
        return True
    except Exception as e:
        print(f"Error creating about.html: {e}")
        return False

if __name__ == "__main__":
    convert_markdown_to_html()