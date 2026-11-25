FROM python:3.11-slim

RUN useradd -m -u 1000 user
WORKDIR /home/user/app

COPY --chown=user:user . .
RUN mkdir -p /home/user/app/data && chown -R user:user /home/user/app/data

USER user

ENV HOME=/home/user \
    PYTHONUNBUFFERED=1 \
    PORT=7860

EXPOSE 7860

CMD ["python3", "-u", "bot.py"]
