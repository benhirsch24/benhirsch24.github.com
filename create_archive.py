#!/usr/bin/env python3

import os
import re
import glob
import datetime
from bs4 import BeautifulSoup
from pathlib import Path

# Configuration
POSTS_DIR = "/Users/ben/benhirsch24.github.com/posts"
ARCHIVE_HTML = "/Users/ben/benhirsch24.github.com/archive.html"
LAYOUT_HTML = "/Users/ben/benhirsch24.github.com/layout.html"

def extract_date_from_filename(filename):
    """Extract date from filename format YYYY-MM-DD-title.html"""
    match = re.search(r'(\d{4}-\d{2}-\d{2})', os.path.basename(filename))
    if match:
        date_str = match.group(1)
        try:
            return datetime.datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return datetime.datetime.now() - datetime.timedelta(days=365)
    return datetime.datetime.now() - datetime.timedelta(days=365)  # Default to old date

def extract_title_and_date_from_html(file_path):
    """Extract title and date from HTML post file"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f, 'html.parser')
            
            # Look for the post title in h1.post-title
            title_elem = soup.select_one('h1.post-title')
            if title_elem:
                title = title_elem.text.strip()
            else:
                # Fallback: look in the first h1 in post-content
                content_h1 = soup.select_one('.post-content h1')
                if content_h1:
                    title = content_h1.text.strip()
                else:
                    # Last resort: use the title tag
                    title_tag = soup.title
                    if title_tag:
                        title = title_tag.text.strip().replace(' - Ben Hirsch', '')
                    else:
                        title = os.path.basename(file_path).replace('.html', '').replace('-', ' ').title()
            
            # Look for date in .post-info
            date_elem = soup.select_one('.post-info')
            date_str = None
            if date_elem:
                date_str = date_elem.text.strip()
            
            # Use filename date as fallback
            date = extract_date_from_filename(file_path)
            
            return {
                'title': title,
                'date': date,
                'date_str': date_str if date_str else date.strftime("%B %d, %Y"),
                'path': file_path,
                'rel_path': os.path.join('posts', os.path.basename(file_path))
            }
    except Exception as e:
        print(f"Error processing {file_path}: {e}")
        return {
            'title': os.path.basename(file_path).replace('.html', '').replace('-', ' ').title(),
            'date': datetime.datetime.now() - datetime.timedelta(days=365),
            'date_str': 'Unknown date',
            'path': file_path,
            'rel_path': os.path.join('posts', os.path.basename(file_path))
        }

def get_all_posts():
    """Get all posts from the posts directory sorted by date"""
    # Get all HTML files
    posts = glob.glob(os.path.join(POSTS_DIR, "*.html"))
    
    # Extract metadata from each post
    post_data = [extract_title_and_date_from_html(post) for post in posts]
    
    # Sort by date (newest first)
    post_data.sort(key=lambda x: x['date'], reverse=True)
    
    return post_data

def generate_post_html(post_data):
    """Generate HTML for a post link"""
    return f"""
    <div class="post">
        <h2 class="post-title"><a href="{post_data['rel_path']}">{post_data['title']}</a></h2>
        <div class="post-info">{post_data['date_str']}</div>
    </div>
    """

def group_posts_by_year(posts):
    """Group posts by year for the archive page"""
    years = {}
    for post in posts:
        year = post['date'].year
        if year not in years:
            years[year] = []
        years[year].append(post)
    
    # Sort years in descending order
    return dict(sorted(years.items(), key=lambda x: x[0], reverse=True))

def read_layout_template():
    """Read the layout template file"""
    try:
        with open(LAYOUT_HTML, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print(f"Error reading layout template: {e}")
        return None

def create_archive_html():
    """Create the archive.html file with links to all posts grouped by year"""
    all_posts = get_all_posts()
    posts_by_year = group_posts_by_year(all_posts)
    
    # Generate HTML for each year and its posts
    archive_html = "<h1>Post Archive</h1>"
    
    for year, posts in posts_by_year.items():
        archive_html += f"<h2>{year}</h2>"
        for post in posts:
            archive_html += f"""
            <div class="post">
                <h3 class="post-title"><a href="{post['rel_path']}">{post['title']}</a></h3>
                <div class="post-info">{post['date_str']}</div>
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
        print(f"Created {ARCHIVE_HTML} with {len(all_posts)} posts")
        return True
    except Exception as e:
        print(f"Error creating archive.html: {e}")
        return False

if __name__ == "__main__":
    create_archive_html()