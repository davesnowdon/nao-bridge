#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Python 2.7.9 Compatibility Checker

This script checks Python files in the server directory for Python 2.7.9 compatibility.
It looks for common Python 3+ features that are not available in Python 2.7.

Usage:
    python2.7 check_python27_compatibility.py [directory]

Author: NAO Bridge Project
"""

from __future__ import print_function
import os
import sys
import re
import argparse

# Python 3+ features that are not available in Python 2.7
PYTHON3_FEATURES = {
    'f-strings': r'f["\']',
    'walrus_operator': r':=',
    'match_statement': r'^\s*match\s+',
    'case_statement': r'^\s*case\s+',
    'type_annotations': r':\s*(str|int|float|bool|list|dict|tuple|set|Optional|Union|Any)\s*[=)]',
    'dataclass_decorator': r'@dataclass',
    'pathlib_import': r'from pathlib import|import pathlib',
    'typing_import': r'from typing import|import typing',
    'async_await': r'async def|await ',
    'assignment_expressions': r'if\s*\(.*:=.*\)',
    'set_literals': r'\{[^}]*,\s*[^}]*\}',  # Basic set literal detection
}

# Required future imports
REQUIRED_FUTURE_IMPORTS = [
    'from __future__ import print_function',
    'from __future__ import division',
    'from __future__ import absolute_import'
]

def check_file(filepath):
    """Check a single Python file for Python 2.7 compatibility."""
    issues = []
    
    try:
        with open(filepath, 'r') as f:
            content = f.read()
            lines = content.split('\n')
    except UnicodeDecodeError:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                lines = content.split('\n')
        except:
            return [('encoding_error', 'Unable to read file encoding')]
    
    # Check for required future imports
    future_imports = []
    for line in lines:
        if line.strip().startswith('from __future__ import'):
            future_imports.append(line.strip())
    
    missing_imports = []
    for required in REQUIRED_FUTURE_IMPORTS:
        if not any(required in imp for imp in future_imports):
            missing_imports.append(required)
    
    if missing_imports:
        issues.append(('missing_future_imports', 'Missing: ' + ', '.join(missing_imports)))
    
    # Check for Python 3+ features
    for feature_name, pattern in PYTHON3_FEATURES.items():
        matches = re.finditer(pattern, content, re.MULTILINE)
        for match in matches:
            line_num = content[:match.start()].count('\n') + 1
            line_content = lines[line_num - 1].strip()
            issues.append((
                feature_name,
                'Line {}: {}'.format(line_num, line_content[:50] + '...' if len(line_content) > 50 else line_content)
            ))
    
    # Check for print statements without parentheses
    print_pattern = r'^\s*print\s+[^(]'
    matches = re.finditer(print_pattern, content, re.MULTILINE)
    for match in matches:
        line_num = content[:match.start()].count('\n') + 1
        line_content = lines[line_num - 1].strip()
        issues.append((
            'print_statement',
            'Line {}: {}'.format(line_num, line_content[:50] + '...' if len(line_content) > 50 else line_content)
        ))
    
    # Check for dictionary comprehensions
    dict_comp_pattern = r'\{[^}]*for[^}]*in[^}]*\}'
    matches = re.finditer(dict_comp_pattern, content, re.MULTILINE)
    for match in matches:
        line_num = content[:match.start()].count('\n') + 1
        line_content = lines[line_num - 1].strip()
        issues.append((
            'dict_comprehension',
            'Line {}: {}'.format(line_num, line_content[:50] + '...' if len(line_content) > 50 else line_content)
        ))
    
    return issues

def check_directory(directory):
    """Check all Python files in a directory for Python 2.7 compatibility."""
    all_issues = {}
    
    for root, dirs, files in os.walk(directory):
        # Skip lib directory as it contains third-party code
        if 'lib' in root:
            continue
            
        for file in files:
            if file.endswith('.py'):
                filepath = os.path.join(root, file)
                issues = check_file(filepath)
                if issues:
                    all_issues[filepath] = issues
    
    return all_issues

def main():
    parser = argparse.ArgumentParser(description='Check Python 2.7.9 compatibility')
    parser.add_argument('directory', nargs='?', default='.', 
                       help='Directory to check (default: current directory)')
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='Verbose output')
    
    args = parser.parse_args()
    
    print("Checking Python 2.7.9 compatibility in: {}".format(args.directory))
    print("=" * 60)
    
    issues = check_directory(args.directory)
    
    if not issues:
        print("✅ All Python files are compatible with Python 2.7.9!")
        return 0
    
    print("❌ Found compatibility issues:")
    print()
    
    exit_code = 1
    
    for filepath, file_issues in issues.items():
        print("📁 {}".format(filepath))
        for issue_type, description in file_issues:
            print("  ❌ {}: {}".format(issue_type, description))
        print()
    
    print("Total files with issues: {}".format(len(issues)))
    print()
    print("Please refer to PYTHON27_RULES.md for guidance on fixing these issues.")
    
    return exit_code

if __name__ == '__main__':
    sys.exit(main()) 