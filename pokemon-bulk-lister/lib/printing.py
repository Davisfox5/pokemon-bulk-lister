"""Explicit card finishes. Never substitute a different finish's price."""
import re

def normalize(value=None, is_holo=False):
    if value in (None, ''):
        return 'holofoil' if is_holo else 'normal'
    text = re.sub(r'[^a-z0-9]', '', str(value).lower())
    return {
        'normal': 'normal', 'nonholo': 'normal', 'nonholofoil': 'normal',
        'holo': 'holofoil', 'holofoil': 'holofoil', 'holographic': 'holofoil',
        'reverse': 'reverseHolofoil', 'reverseholo': 'reverseHolofoil',
        'reverseholofoil': 'reverseHolofoil',
    }.get(text, 'unknown')


def of(card):
    return normalize(card.get('printing'), card.get('is_holo', False))
