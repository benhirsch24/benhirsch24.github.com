#!/usr/bin/env python3

import os
import re
import glob
import datetime
import markdown
from bs4 import BeautifulSoup
from pathlib import Path

# Configuration
POSTS_MD_DIR = "/Users/ben/benhirsch24.github.com/posts_md"
POSTS_DIR = "/Users/ben/benhirsch24.github.com/posts"
INDEX_HTML = "/Users/ben/benhirsch24.github.com/index.html"
ARCHIVE_HTML = "/Users/ben/benhirsch24.github.com/archive.html"
ABOUT_MD = "/Users/ben/benhirsch24.github.com/about.md"
ABOUT_HTML = "/Users/ben/benhirsch24.github.com/about.html"
LAYOUT_HTML = "/Users/ben/benhirsch24.github.com/layout.html"
MAX_POSTS = 5

# Ensure posts directory exists
os.makedirs(POSTS_DIR, exist_ok=True)

def read_layout_template():
    """Read the layout template file"""
    try:
        with open(LAYOUT_HTML, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print(f"Error reading layout template: {e}")
        return None

def extract_metadata(content):
    """Extract metadata from markdown frontmatter."""
    metadata = {}

    # Check for frontmatter (---) at the beginning
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if match:
        frontmatter = match.group(1)
        # Extract key-value pairs
        for line in frontmatter.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                metadata[key.strip()] = value.strip()

        # Remove the frontmatter from the content
        content = content[match.end():]

    return metadata, content

def extract_title_from_content(content):
    """Extract title from the first heading"""
    lines = content.split('\n')
    for line in lines:
        if line.startswith('# '):
            return line[2:].strip()
    return "Untitled"

def extract_first_paragraph(content):
    """Extract the first paragraph text from markdown content.
    Skips headings and blank lines."""
    lines = content.split('\n')
    paragraph_lines = []

    # Skip any headings (# heading) at the beginning
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].strip().startswith('#')):
        i += 1

    # Collect lines until we hit an empty line or another heading
    while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith('#'):
        paragraph_lines.append(lines[i].strip())
        i += 1

    # Join the lines and limit the preview length
    paragraph = ' '.join(paragraph_lines).strip()
    if len(paragraph) > 250:
        paragraph = paragraph[:247] + '...'

    return paragraph

def format_date(date_str):
    """Format date string into a more readable format."""
    try:
        date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        return date_obj.strftime("%B %d, %Y")
    except ValueError:
        return date_str

def convert_md_to_html(md_path, layout_template):
    """Convert a markdown file to HTML and insert into template."""
    # Read the markdown file
    with open(md_path, 'r', encoding='utf-8') as f:
        md_content = f.read()

    # Extract metadata
    metadata, md_content = extract_metadata(md_content)

    # Get title from first heading if not in metadata
    title = metadata.get('title', extract_title_from_content(md_content))

    # Get date from metadata or filename
    date = metadata.get('Date', os.path.basename(md_path).split('_')[0])
    formatted_date = format_date(date)

    # Extract first paragraph for preview
    preview = extract_first_paragraph(md_content)

    # Convert markdown to HTML
    html_content = markdown.markdown(
        md_content,
        extensions=['markdown.extensions.fenced_code',
                   'markdown.extensions.tables']
    )

    # Wrap content in article tag with post styling
    post_html = f"""
    <article class="post">
        <h1 class="post-title">{title}</h1>
        <div class="post-info">{formatted_date}</div>
        <div class="post-content">
            {html_content}
        </div>
    </article>
    """

    # Adjust CSS path for posts (which are in a subdirectory)
    adjusted_layout = layout_template.replace('href="./css/main.css"', 'href="../css/main.css"')

    # Insert into layout template
    final_html = adjusted_layout.replace('{{ content }}', post_html)
    final_html = final_html.replace('{{ page_title }}', title)

    # Generate output filename
    filename = os.path.basename(md_path).split('_')[0]
    if '_' in os.path.basename(md_path):
        slug = '_'.join(os.path.basename(md_path).split('_')[1:]).replace('.md', '')
    else:
        slug = os.path.basename(md_path).replace('.md', '')

    output_path = f"posts/{filename}-{slug}.html"

    # Create the output file
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(final_html)

    return {
        'path': output_path,
        'title': title,
        'date': date,
        'date_str': formatted_date,
        'preview': preview
    }

def process_markdown_posts():
    """Process all markdown files in posts_md directory"""
    # Read the layout template
    layout_template = read_layout_template()
    if not layout_template:
        print("Error: Layout template not found or couldn't be read.")
        return []

    # Get all markdown files
    md_files = glob.glob(os.path.join(POSTS_MD_DIR, "*.md"))

    # Process each file
    processed_files = []
    for md_file in md_files:
        post_info = convert_md_to_html(md_file, layout_template)
        processed_files.append(post_info)
        print(f"Processed {md_file} -> {post_info['path']}")

    # Sort by date (newest first)
    processed_files.sort(key=lambda x: x['date'], reverse=True)

    return processed_files

def create_index_html(posts):
    """Create the index.html file with links to recent posts"""
    # Use up to MAX_POSTS
    recent_posts = posts[:MAX_POSTS]

    # Generate HTML for the recent posts
    posts_html = ""
    for post in recent_posts:
        preview = post.get('preview', '')
        html_preview = markdown.markdown(
            preview,
            extensions=['markdown.extensions.fenced_code', 'markdown.extensions.tables']
        )
        posts_html += f"""
    <div class="post">
        <h2 class="post-title"><a href="{post['path']}">{post['title']}</a></h2>
        <div class="post-info">{post['date_str']}</div>
        <div class="post-preview">
            <p>{html_preview}</p>
            <a href="{post['path']}" class="jump">Continue reading →</a>
        </div>
    </div>
    """

    # Wrap the posts HTML in a container
    content_html = f"""
    <section class="recent-posts">
        <h1>Recent Posts</h1>
        {posts_html}
        <p><a href="./archive.html">View all posts</a></p>
    </section>
    """

    # Read the layout template
    layout_template = read_layout_template()
    if not layout_template:
        print("Error: Couldn't read layout template")
        return False

    # Insert the content into the layout template
    index_html = layout_template.replace("{{ content }}", content_html)
    index_html = index_html.replace("{{ page_title }}", "Home")

    # Write the updated HTML to index.html
    try:
        with open(INDEX_HTML, 'w', encoding='utf-8') as f:
            f.write(index_html)
        print(f"Created {INDEX_HTML} with {len(recent_posts)} recent posts")
        return True
    except Exception as e:
        print(f"Error creating index.html: {e}")
        return False

def group_posts_by_year(posts):
    """Group posts by year for the archive page"""
    years = {}
    for post in posts:
        # Extract year from date string
        if isinstance(post['date'], str) and len(post['date']) >= 4:
            year = post['date'][:4]
        else:
            year = "Unknown"

        if year not in years:
            years[year] = []
        years[year].append(post)

    # Sort years in descending order
    return dict(sorted(years.items(), key=lambda x: x[0], reverse=True))

def create_archive_html(posts):
    """Create the archive.html file with links to all posts grouped by year"""
    posts_by_year = group_posts_by_year(posts)

    # Generate HTML for each year and its posts
    archive_html = "<h1>Post Archive</h1>"

    for year, year_posts in posts_by_year.items():
        archive_html += f"<h2>{year}</h2>"
        for post in year_posts:
            preview = post.get('preview', '')
            archive_html += f"""
            <div class="post">
                <h3 class="post-title"><a href="{post['path']}">{post['title']}</a></h3>
                <div class="post-info">{post['date_str']}</div>
                <div class="post-preview">
                    <p>{preview}</p>
                    <a href="{post['path']}" class="jump">Continue reading →</a>
                </div>
            </div>
            """

    # Read the layout template
    layout_template = read_layout_template()
    if not layout_template:
        print("Error: Couldn't read layout template")
        return False

    # Insert the archive content into the layout template
    archive_page = layout_template.replace("{{ content }}", archive_html)
    archive_page = archive_page.replace("{{ page_title }}", "Archive")

    # Write the updated HTML to archive.html
    try:
        with open(ARCHIVE_HTML, 'w', encoding='utf-8') as f:
            f.write(archive_page)
        print(f"Created {ARCHIVE_HTML} with {len(posts)} posts")
        return True
    except Exception as e:
        print(f"Error creating archive.html: {e}")
        return False

def create_about_html():
    """Convert about.md to HTML using the layout template"""
    # Read the markdown content
    try:
        with open(ABOUT_MD, 'r', encoding='utf-8') as f:
            md_content = f.read()
    except Exception as e:
        print(f"Error reading about.md: {e}")
        return False

    # Extract title from the first heading
    title = extract_title_from_content(md_content)

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

def main():
    """Main function to build the entire site."""
    print("Building site...")

    # Process markdown posts
    posts = process_markdown_posts()
    print(f"\nConverted {len(posts)} markdown files to HTML.")

    # Create index.html
    create_index_html(posts)

    # Create archive.html
    create_archive_html(posts)

    # Create about.html
    create_about_html()

    print("\nSite build complete!")

if __name__ == "__main__":
    main()
