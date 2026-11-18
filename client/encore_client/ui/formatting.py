def format_ms(ms: int | None) -> str:
    if ms is None:
        return "--:--"
    seconds = int(ms) // 1000
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"
