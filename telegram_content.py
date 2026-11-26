"""Read Telegram Rich Message article blocks without rendering them."""
from telethon import types

from giveaway import MOSCOW


def rich_text(node, buttons=None):
    if isinstance(node, str):
        return node
    if isinstance(node, types.TextConcat):
        return ''.join(rich_text(part, buttons) for part in node.texts)
    if isinstance(node, types.TextCustomEmoji):
        return node.alt
    if isinstance(node, types.TextDate) and node.date:
        return node.date.astimezone(MOSCOW).strftime('%d.%m.%Y в %H:%M')
    text = rich_text(node.text, buttons) if getattr(node, 'text', None) is not None else ''
    if isinstance(node, types.TextButton) and buttons is not None:
        buttons.append(types.KeyboardInlineButton(text, node.type))
    if isinstance(node, types.TextUrl) and node.url and node.url != text:
        return f'{text}({node.url})'
    return text


def rich_content(message):
    """Return the article text and its inline buttons in Telegram order."""
    article = getattr(message, 'rich_message', None)
    lines, buttons = [], []

    def add_text(node):
        text = rich_text(node, buttons).strip()
        if text:
            lines.append(text)

    def visit(block):
        if isinstance(block, types.PageBlockButtonRow):
            buttons.extend(types.KeyboardInlineButton(rich_text(button.text), button.type)
                           for button in block.buttons)
            return
        add_text(getattr(block, 'title', None))
        add_text(getattr(block, 'text', None))
        caption = getattr(block, 'caption', None)
        add_text(caption)
        if isinstance(caption, types.PageCaption):
            add_text(caption.credit)
        if isinstance(block, types.PageBlockCover):
            visit(block.cover)
        for child in getattr(block, 'blocks', ()) or getattr(block, 'items', ()):
            visit(child)
        if isinstance(block, types.PageBlockTable):
            for row in block.rows:
                for cell in row.cells:
                    add_text(cell.text)

    for block in getattr(article, 'blocks', ()):
        visit(block)
    return '\n'.join(lines), buttons


def giveaway_buttons(message, article_buttons):
    markup = getattr(message, 'reply_markup', None)
    rows = ([row.buttons for row in markup.rows] if isinstance(markup, types.ReplyInlineMarkup)
            else getattr(message, 'buttons', None))
    candidates = [button for row in (rows or []) for button in row] + article_buttons
    return [button for button in candidates
            if isinstance(getattr(button, 'type', None), types.InlineButtonTypeCallback)
            and not button.type.requires_password]
