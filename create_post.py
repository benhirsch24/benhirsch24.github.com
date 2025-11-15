#!/usr/bin/env python3

import sys
import os
import datetime
from slugify import slugify
import argparse

def create_post(title):
    # Create slug from title
    slug = slugify(title)

    # Create date in required format: YYYY-MM-DD
    today = datetime.datetime.now()
    date_str = today.strftime("%Y-%m-%d")

    # Create filename with date and slug
    filename = f"{date_str}_{slug}.md"
    filepath = os.path.join("drafts", filename)

    # Create basic post content
    content = f"""---
Date: {date_str}
---

# {title}

"""

    # Create the file
    with open(filepath, "w") as f:
        f.write(content)

    print(f"Created new post: {filepath}")
    return filepath

def main():
    parser = argparse.ArgumentParser(description="Create a new blog post")
    parser.add_argument("title", help="Title of the blog post")

    args = parser.parse_args()

    if not args.title:
        print("Error: Title is required")
        sys.exit(1)

    create_post(args.title)

if __name__ == "__main__":
    main()
