#!/usr/bin/env python3

import os
import re
import glob
import datetime
from bs4 import BeautifulSoup
from pathlib import Path

# Configuration
POSTS_DIR = "/Users/ben/benhirsch24.github.com/posts"
INDEX_HTML = "/Users/ben/benhirsch24.github.com/index.html"
LAYOUT_HTML = "/Users/ben/benhirsch24.github.com/layout.html"
MAX_POSTS = 5

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

def get_recent_posts(max_posts=5):
    """Get the most recent posts from the posts directory"""
    # Get all HTML files
    posts = glob.glob(os.path.join(POSTS_DIR, "*.html"))
    
    # Extract metadata from each post
    post_data = [extract_title_and_date_from_html(post) for post in posts]
    
    # Sort by date (newest first)
    post_data.sort(key=lambda x: x['date'], reverse=True)
    
    # Return the most recent posts
    return post_data[:max_posts]

def generate_post_html(post_data):
    """Generate HTML for a post link"""
    return f"""
    <div class="post">
        <h2 class="post-title"><a href="{post_data['rel_path']}">{post_data['title']}</a></h2>
        <div class="post-info">{post_data['date_str']}</div>
    </div>
    """

def read_layout_template():
    """Read the layout template file"""
    try:
        with open(LAYOUT_HTML, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print(f"Error reading layout template: {e}")
        return None

def update_index_html():
    """Update the index.html file with links to recent posts"""
    recent_posts = get_recent_posts(MAX_POSTS)
    
    # Generate HTML for the recent posts
    posts_html = ""
    for post in recent_posts:
        posts_html += generate_post_html(post)
    
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
        print(f"Updated {INDEX_HTML} with {len(recent_posts)} recent posts")
        return True
    except Exception as e:
        print(f"Error updating index.html: {e}")
        return False

if __name__ == "__main__":
    update_index_html()