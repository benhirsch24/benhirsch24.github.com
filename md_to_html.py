#!/usr/bin/env python3

import os
import re
import glob
import datetime
import markdown
from pathlib import Path
import yaml

# Directories
POSTS_MD_DIR = "/Users/ben/benhirsch24.github.com/posts_md"
POSTS_HTML_DIR = "/Users/ben/benhirsch24.github.com/posts"
LAYOUT_PATH = "/Users/ben/benhirsch24.github.com/layout.html"

# Ensure posts directory exists
os.makedirs(POSTS_HTML_DIR, exist_ok=True)

def read_layout_template():
    """Read the layout template file."""
    try:
        with open(LAYOUT_PATH, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print(f"Error reading layout template: {e}")
        return None

def extract_metadata(content):
    """Extract metadata from markdown frontmatter."""
    metadata = {}
    # Regular frontmatter with --- delimiters
    frontmatter_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    
    if frontmatter_match:
        frontmatter = frontmatter_match.group(1)
        try:
            # Parse YAML frontmatter
            metadata = yaml.safe_load(frontmatter)
            # Remove frontmatter from content
            content = re.sub(r'^---\s*\n.*?\n---\s*\n', '', content, flags=re.DOTALL)
        except Exception as e:
            print(f"Error parsing frontmatter: {e}")
    
    # Handle basic key-value pairs at the top of the file
    if not metadata:
        lines = content.split("\n")
        for line in lines:
            if ":" in line and not line.startswith("#"):
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip()
                # Remove these lines from content
                content = content.replace(line + "\n", "", 1)
            else:
                break
    
    return metadata, content

def extract_title(metadata, content):
    """Extract title from metadata or first heading."""
    if 'title' in metadata:
        return metadata['title']
    
    if 'Title' in metadata:
        return metadata['Title']
    
    # Try to extract from first heading
    heading_match = re.search(r'^#\s+(.*?)$', content, re.MULTILINE)
    if heading_match:
        return heading_match.group(1)
    
    return "Untitled"

def extract_date(metadata, filename):
    """Extract date from metadata or filename."""
    # Try to get date from metadata
    for key in ['date', 'Date']:
        if key in metadata:
            return metadata[key]
    
    # Try to extract date from filename (format: YYYY-MM-DD_title.md or YYYY-MM-DD_HH_MM_SS_title.md)
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
    if date_match:
        return date_match.group(1)
    
    # Alternative format: YYYY-MM-DD_HH_MM_title.md
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})_(\d{2})_(\d{2})', filename)
    if date_match:
        return date_match.group(1)
    
    return datetime.datetime.now().strftime("%Y-%m-%d")

def format_date(date_str):
    """Format date in a nice way."""
    try:
        # Try to parse the date string
        if isinstance(date_str, str):
            # Remove any time component if present
            date_str = date_str.split()[0]
            date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            return date_obj.strftime("%B %d, %Y")
    except Exception as e:
        print(f"Error formatting date '{date_str}': {e}")
    
    return date_str  # Return original if parsing fails

def convert_markdown_to_html(md_file_path, layout_template):
    """Convert a markdown file to HTML and insert into template."""
    try:
        file_basename = os.path.basename(md_file_path)
        
        with open(md_file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Extract metadata and clean content
        metadata, clean_content = extract_metadata(content)
        
        # Extract title and date
        title = extract_title(metadata, clean_content)
        date_str = extract_date(metadata, file_basename)
        formatted_date = format_date(date_str)
        
        # Check if the content starts with a title that matches the extracted title
        # If so, remove the first title heading to avoid duplication
        lines = clean_content.split('\n')
        if lines and lines[0].startswith('# ') and lines[0][2:].strip() == title:
            clean_content = '\n'.join(lines[1:])
        
        # Convert markdown to HTML
        html_content = markdown.markdown(
            clean_content,
            extensions=[
                'markdown.extensions.fenced_code',
                'markdown.extensions.tables',
                'markdown.extensions.codehilite'
            ]
        )
        
        # Wrap the HTML content with date and title
        wrapped_html = f"""
        <article class="post">
            <h1 class="post-title">{title}</h1>
            <div class="post-info">{formatted_date}</div>
            <div class="post-content">
                {html_content}
            </div>
        </article>
        """
        
        # Insert content into layout
        html = layout_template.replace("{{ content }}", wrapped_html)
        html = html.replace("{{ page_title }}", title)
        
        # Generate output filename
        output_filename = os.path.splitext(file_basename)[0] + ".html"
        
        # For filenames with timestamp, clean them up
        if re.search(r'\d{4}-\d{2}-\d{2}_\d{2}_\d{2}', output_filename):
            # Extract date part
            date_part = re.search(r'(\d{4}-\d{2}-\d{2})', output_filename).group(1)
            # Extract title part (anything after the timestamp)
            title_part = re.sub(r'\d{4}-\d{2}-\d{2}_\d{2}_\d{2}(_\d{2})?_', '', output_filename)
            output_filename = f"{date_part}-{title_part}"
        
        output_path = os.path.join(POSTS_HTML_DIR, output_filename)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)
        
        print(f"Converted {file_basename} to {output_filename}")
        return True
        
    except Exception as e:
        print(f"Error converting {md_file_path}: {e}")
        return False

def main():
    """Main function to convert all markdown files."""
    # Read layout template
    layout_template = read_layout_template()
    if not layout_template:
        print("Error: Layout template not found or couldn't be read.")
        return
    
    # Get all markdown files
    md_files = glob.glob(os.path.join(POSTS_MD_DIR, "*.md"))
    
    if not md_files:
        print(f"No markdown files found in {POSTS_MD_DIR}")
        return
    
    success_count = 0
    for md_file in md_files:
        if convert_markdown_to_html(md_file, layout_template):
            success_count += 1
    
    print(f"Converted {success_count} of {len(md_files)} markdown files to HTML.")

if __name__ == "__main__":
    main()