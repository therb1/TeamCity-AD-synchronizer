import logging, os
from settings import log_file, max_size
from logging.handlers import RotatingFileHandler

file_name = log_file
FORMAT = "[%(asctime)s] [%(levelname)5.5s] %(message)s"
consoleLevel = logging.INFO
fileLevel = logging.DEBUG

def initLog():
    formatter = logging.Formatter(FORMAT)
    logger = logging.getLogger('root')
    fileHandler = RotatingFileHandler(file_name, 'a', maxBytes= max_size*1024*1024, backupCount=2)
    fileHandler.setFormatter(formatter)
    consoleHandler = logging.StreamHandler()
    consoleHandler.setFormatter(formatter)
    consoleHandler.setLevel(consoleLevel)
    fileHandler.setLevel(fileLevel)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(fileHandler)
    logger.addHandler(consoleHandler)
    logger.info("Logging is enabled")
    return logger
