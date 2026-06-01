import re


def slugify(text: str) -> str:
    """Convert a string into a URL-friendly slug.
    
    Args:
        text: The input string to convert.
    
    Returns:
        A URL-friendly slug string.
    """
    # 1. Convert to lowercase
    slug = text.lower()
    
    # 2. Replace non-alphanumeric characters (excluding hyphens) with hyphens
    slug = re.sub(r'[^a-z0-9-]', '-', slug)
    
    # 3. Collapse multiple consecutive hyphens into one
    slug = re.sub(r'-+', '-', slug)
    
    # 4. Strip leading/trailing hyphens
    slug = slug.strip('-')
    
    return slug


if __name__ == "__main__":
    examples = [
        "Hello World",
        "Hello, World!",
        "  leading and trailing  ",
        "already-a-slug",
        "Python 3.9 is great!",
        "",
        "---multiple---hyphens---",
    ]
    
    for text in examples:
        result = slugify(text)
        print(f"{text!r} -> {result!r}")
