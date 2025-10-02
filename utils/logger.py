import logging
import os
import sys
import tempfile


_loggers = {}

def setup_logger(name="citeverify", level=logging.INFO, toFile=True, fileName="citeverify.log"):
    """
    Establish an instance of a logger to be used for logging in current context of app

    Args
        name: name of the logger
        level: level of logging info
        toFile: 

    """
    if name in _loggers:
        return _loggers[name]

    # Use a named logger instead of root to avoid polluting global handlers
    logger = logging.getLogger(name)
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    logger.setLevel(numeric_level)
    formatter = logging.Formatter("[%(asctime)s] - %(name)s %(levelname)s %(message)s")

    # Avoid adding duplicate handlers (e.g., when reloading in dev servers)
    if logger.handlers:
        _loggers[name] = logger
        return logger

    # Determine if file logging is requested via param or env
    env_to_file = os.getenv("LOG_TO_FILE", str(toFile)).lower() in {"1", "true", "yes", "on"}
    target_file = os.getenv("LOG_FILE_PATH", fileName)

    if env_to_file:
        # Resolve a writable path. HF Spaces code dir (/app) may be read-only; prefer /data then /tmp
        base_dir = os.path.dirname(target_file) or "."
        if not os.path.isabs(target_file):
            # If relative and not writable, we'll adjust later
            candidate_paths = [base_dir]
        else:
            candidate_paths = [base_dir]
        # Append common writable dirs if original isn't writable
        candidate_paths.extend(["/data", tempfile.gettempdir()])

        selected_path = None
        for p in candidate_paths:
            try:
                if not os.path.isdir(p):
                    continue
                if os.access(p, os.W_OK):
                    selected_path = p
                    break
            except Exception:
                continue

        if selected_path:
            final_path = target_file if os.path.isabs(target_file) else os.path.join(selected_path, os.path.basename(target_file))
            try:
                file_handler = logging.FileHandler(final_path)
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)
            except PermissionError:
                logger.error("File logging disabled: no write permission for %s", final_path)
            except OSError as e:
                logger.error("File logging disabled: %s", e)
        else:
            logger.error("File logging disabled: no writable directory found for %s", target_file)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    _loggers[name] = logger
    return logger

def get_logger(name="citeverify"):
    return logging.getLogger(name)
