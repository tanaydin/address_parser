import asyncio
import logging
import os
import re
from functools import lru_cache
from typing import List, Optional
from fastapi import FastAPI, Request, HTTPException
import src.converter as converter
from src.config import Settings
from src.logger import setup_logging
from src.models import IntentResponse, RequestIntent
from src.lm.tokenizer import GPTTokenizer


setup_logging()
app = FastAPI()
rotator = 0
lock = asyncio.Lock()

@lru_cache(maxsize=None)
def get_settings(pid: int):
    settings = Settings()

    with open(settings.address_prompt_file) as handle:
        settings.address_template = handle.read()

    with open(settings.detailed_intent_prompt_file) as handle:
        settings.detailed_intent_template = handle.read()

    if settings.geo_location:
        settings.geo_key = converter.setup_geocoding()

    settings.openai_keys = converter.setup_openai(pid % settings.num_workers)

    logging.warning(f"Engine {settings.engine}")

    return settings


async def convert(
        info: str,
        inputs: List[str],
        settings: Settings,
        api_key: Optional[str] = None,
):
    if info == "address":
        template = settings.address_template
        max_tokens = settings.address_max_tokens
        temperature = 0.1
        frequency_penalty = 0.3
    elif info == "detailed_intent":
        template = settings.detailed_intent_template
        max_tokens = settings.detailed_intent_max_tokens
        temperature = 0.0
        frequency_penalty = 0.0
    else:
        raise ValueError("Unknown information extraction requested")

    def preprocess_tweet(text: str) -> str:
        mention_pattern = r"@\w+"
        url_pattern = r"(\w+?://)?(?:www\.)?[-a-zA-Z0-9@:%._\\+~#=]{1,256}\.[a-zA-Z]{1,10}\b(?:[-a-zA-Z0-9()@:%_\\+.~#?&\\/=]*)"
        # remove mentions
        mentions_removed = re.sub(mention_pattern, " ", text)
        # remove urls
        url_removed = re.sub(url_pattern, "", mentions_removed)
        # remove consequent spaces
        return re.sub(r"\s+", " ", url_removed)

    def create_prompt(text: str, template: str, max_tokens: int) -> str:
        template_token_count = GPTTokenizer.token_count(template)

        preprocessed_text = preprocess_tweet(text)

        truncated_text = GPTTokenizer.truncate(
            preprocessed_text,
            max_tokens=GPTTokenizer.MAX_TOKENS - max_tokens - template_token_count,
        )

        return template.format(ocr_input=truncated_text)

    text_inputs = []
    for tweet in inputs:
        text_inputs.append(create_prompt(text=tweet, template=template, max_tokens=max_tokens))

    outputs = await converter.query_with_retry(
        text_inputs,
        api_key=api_key,
        engine=settings.engine,
        temperature=temperature,
        max_tokens=max_tokens,
        top_p=1,
        frequency_penalty=frequency_penalty,
        presence_penalty=0,
        stop="#END",
    )

    returned = []
    for output in outputs:
        returned_dict = {}
        returned_dict["string"] = output
        try:
            returned_dict["processed"] = converter.postprocess(info, output[0])
        except Exception as e:
            returned_dict["processed"] = {
                "intent": [],
                "detailed_intent_tags": [],
            }
            logging.warning(f"Parsing error in {output},\n {e}")

        if info == "address" and settings.geo_location and returned_dict["processed"]:
            returned_dict["processed"]["geo"] = converter.get_geo_result(
                settings.geo_key, returned_dict["processed"]
            )
        returned.append(returned_dict)

    return returned


@app.post("/intent-extractor/", response_model=IntentResponse)
async def intent(payload: RequestIntent, req: Request):
    correct_token = os.getenv("NEEDS_RESOLVER_API_KEY", None)
    if correct_token is None:
        raise Exception("token not found in env files!")
    coming_token = req.headers["Authorization"]
    # Here your code for verifying the token or whatever you use
    if coming_token != 'Bearer ' + correct_token:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )

    settings = get_settings(os.getpid())

    inputs = payload.dict()["inputs"]

    global rotator
    async with lock:
        rotator = (rotator + 1) % len(settings.openai_keys)

    api_key = settings.openai_keys[rotator]

    outputs = await convert("detailed_intent", inputs, settings, api_key=api_key)
    return {"response": outputs}


@app.get("/health")
async def health():
    return {"status": "living the dream"}