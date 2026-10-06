"""Small provider adapters; keys and raw provider errors never reach the client."""
from __future__ import annotations

import json
import copy
import time
from urllib.parse import urlsplit
from urllib.parse import quote
import requests
from .assistant_audit import usage


def validate_endpoint(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Enter an HTTP or HTTPS API base URL without credentials, query or fragment.')
    if len(value) > 500:
        raise ValueError('API base URL is too long.')
    return value.rstrip('/')


def request_provider(config, path, on_delta=None, **kwargs):
    headers = {'Authorization': 'Bearer ' + config['api_key']} if config.get('api_key') else {}
    if config['api_type'] == 'anthropic' and path == '/messages':
        headers = {'x-api-key': config.get('api_key', ''), 'anthropic-version': '2023-06-01'}
    elif config['api_type'] == 'gemini' and path.startswith('/models/'):
        headers = {'x-goog-api-key': config.get('api_key', '')}
    try:
        with requests.Session() as client:
            client.trust_env = False
            with client.post(config['base_url'] + path, headers=headers, timeout=(10, config['timeout']),
                             allow_redirects=False, stream=True, **kwargs) as response:
                if not 200 <= response.status_code < 300:
                    raise ValueError(f'AI provider returned HTTP {response.status_code}. Check endpoint, model and credentials.')
                if on_delta is not None and 'text/event-stream' in response.headers.get('Content-Type', ''):
                    return read_stream(response, path, config['timeout'], on_delta)
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 2 * 1024 * 1024:
                        raise ValueError('AI provider response exceeded the size limit.')
                    chunks.append(chunk)
                return json.loads(b''.join(chunks))
    except (requests.RequestException, json.JSONDecodeError) as exc:
        raise ValueError('AI provider could not complete the request. Check the connection and retry.') from exc


def read_stream(response, path, timeout, on_delta):
    deadline, size = time.monotonic() + timeout, 0
    text_parts, calls, measured = [], {}, {}
    native = dict(content=[], usage={})
    for raw in response.iter_lines(chunk_size=64):
        size += len(raw)
        if size > 2 * 1024 * 1024 or time.monotonic() > deadline:
            raise ValueError('AI provider streaming response exceeded its limit.')
        if not raw.startswith(b'data:'):
            continue
        data = raw[5:].strip()
        if data == b'[DONE]':
            break
        event = json.loads(data)
        if path == '/messages':
            kind = event.get('type')
            if kind == 'message_start':
                native.update(event['message'])
            elif kind == 'content_block_start':
                calls[event['index']] = dict(event['content_block'], _json='')
            elif kind == 'content_block_delta':
                block, delta = calls[event['index']], event['delta']
                if delta['type'] == 'text_delta':
                    block['text'] = block.get('text', '') + delta['text']; on_delta(delta['text'])
                elif delta['type'] == 'input_json_delta':
                    block['_json'] += delta['partial_json']
                elif delta['type'] == 'thinking_delta':
                    block['thinking'] = block.get('thinking', '') + delta['thinking']
                elif delta['type'] == 'signature_delta':
                    block['signature'] = block.get('signature', '') + delta['signature']
            elif kind == 'message_delta':
                native['usage'].update(event.get('usage', {})); native.update(event.get('delta', {}))
            elif kind == 'error':
                raise ValueError('The AI provider could not finish its response.')
            elif kind == 'message_stop':
                for block in calls.values():
                    fragment = block.pop('_json', '')
                    if fragment: block['input'] = json.loads(fragment)
                native['content'] = list(calls.values())
                return native
            continue
        if path.startswith('/models/'):
            if event.get('error'): raise ValueError('The AI provider could not finish its response.')
            native.update(event)
            for candidate in event.get('candidates', []):
                for part in candidate.get('content', {}).get('parts', []):
                    native.setdefault('_parts', []).append(part)
                    if part.get('text') and not part.get('thought'): on_delta(part['text'])
            continue
        if path == '/responses':
            if event.get('type') == 'response.output_text.delta':
                on_delta(event.get('delta', ''))
            elif event.get('type') == 'response.completed':
                return event['response']
            elif event.get('type') in ('error', 'response.failed', 'response.incomplete'):
                raise ValueError('The AI provider could not finish its response. Try a shorter request or a higher response limit.')
        else:
            if event.get('usage'): measured = event['usage']
            if event.get('error'):
                raise ValueError('The AI provider could not finish its response.')
            for choice in event.get('choices', []):
                delta = choice.get('delta', {})
                if delta.get('content'):
                    text_parts.append(delta['content'])
                    on_delta(delta['content'])
                for call in delta.get('tool_calls', []):
                    item = calls.setdefault(call['index'], dict(id='', type='function', function=dict(name='', arguments='')))
                    if call.get('id'):
                        item['id'] = call['id']
                    for field in ('name', 'arguments'):
                        item['function'][field] += call.get('function', {}).get(field, '')
                if choice.get('finish_reason') == 'length':
                    raise ValueError('The AI response reached its token limit. Increase the response limit or simplify your request.')
    if path == '/responses':
        raise ValueError('The AI response stream ended before completion.')
    if path == '/messages': raise ValueError('The AI response stream ended before completion.')
    if path.startswith('/models/'):
        candidates = native.get('candidates', [])
        native['candidates'] = [dict(content=dict(parts=native.pop('_parts', [])), finishReason=candidates[0].get('finishReason') if candidates else None)]
        return native
    return dict(usage=measured, choices=[dict(message=dict(content=''.join(text_parts), tool_calls=list(calls.values())))])


def completed_tool_history(history):
    """Keep cancelled/failed multi-tool turns valid for the next provider call."""
    result=[];index=0
    while index<len(history):
        message=history[index];result.append(message);index+=1
        calls=message.get('tool_calls',[]) if message['role']=='assistant' else []
        if not calls: continue
        completed=set()
        while index<len(history) and history[index]['role']=='tool':
            output=history[index];result.append(output);completed.add(output.get('tool_call_id'));index+=1
        for call in calls:
            if call.get('id') not in completed:
                result.append(dict(role='tool',tool_call_id=call['id'],content=json.dumps(dict(error='This operation was interrupted; no result was received. Check current app state before retrying an action.'))))
    return result


def complete(config, history, tools, system, on_delta=None):
    history=completed_tool_history(history)
    if not config.get('streaming', True):
        on_delta = None
    if config['api_type'] in ('anthropic', 'gemini'):
        return complete_native(config, history, tools, system, on_delta)
    if config['api_type'] == 'responses':
        wire = []
        for message in history:
            if message.get('_response_output') is not None:
                wire.extend(message['_response_output'])
            elif message['role'] == 'tool':
                wire.append({'type': 'function_call_output', 'call_id': message['tool_call_id'], 'output': message['content']})
            else:
                content = message.get('content') or ''
                if message.get('_images'):
                    content = ([dict(type='input_text', text=content)] if content else []) + [dict(type='input_image', image_url=i['data_url']) for i in message['_images']]
                wire.append({'role': message['role'], 'content': content})
                for call in message.get('tool_calls', []):
                    wire.append(dict(type='function_call', call_id=call['id'], name=call['function']['name'], arguments=call['function']['arguments']))
        definitions = [dict(type='function', name=t['name'], description=t['description'], parameters=t['inputSchema'], strict=False) for t in tools]
        payload = dict(model=config['model'], instructions=system, input=wire, tools=definitions,
                       max_output_tokens=config['max_tokens'], store=False, include=['reasoning.encrypted_content'])
        if not tools: payload.pop('tools')
        if on_delta is not None:
            payload['stream'] = True
        result = request_provider(config, '/responses', json=payload, on_delta=on_delta)
        output = result.get('output', [])
        calls = [dict(id=item['call_id'], type='function', function=dict(name=item['name'], arguments=item['arguments']))
                 for item in output if item.get('type') == 'function_call']
        content = '\n'.join(part.get('text', '') for item in output if item.get('type') == 'message'
                            for part in item.get('content', []) if part.get('type') == 'output_text')
        return dict(role='assistant', content=content, tool_calls=calls, _response_output=output, _usage=usage(config, result.get('usage')))
    definitions = [dict(type='function', function=dict(name=t['name'], description=t['description'], parameters=t['inputSchema'])) for t in tools]
    wire = [{k: v for k, v in m.items() if k in ('role', 'content', 'tool_calls', 'tool_call_id')} for m in history]
    for original, m in zip(history, wire):
        if original.get('_images'):
            m['content'] = ([dict(type='text', text=m['content'])] if m.get('content') else []) + [dict(type='image_url', image_url=dict(url=i['data_url'])) for i in original['_images']]
        if not m.get('tool_calls'):
            m.pop('tool_calls', None)
    payload = dict(model=config['model'], messages=[dict(role='system', content=system)] + wire,
                   tools=definitions, max_tokens=config['max_tokens'])
    if not tools: payload.pop('tools')
    if on_delta is not None:
        payload['stream'] = True
        payload['stream_options'] = {'include_usage': True}
    result = request_provider(config, '/chat/completions', json=payload, on_delta=on_delta)
    try:
        message = result['choices'][0]['message']
        return dict(role='assistant', content=message.get('content') or '', tool_calls=message.get('tool_calls') or [], _usage=usage(config, result.get('usage')))
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError('The endpoint did not return a compatible chat response.') from exc


def test_connection(config):
    tool = dict(name='connection_check', description='Call this function to verify tool support.',
                inputSchema=dict(type='object', properties={}, additionalProperties=False))
    result = complete(config, [dict(role='user', content='Call connection_check once. Do not answer with text.')], [tool], 'Test the connection by calling connection_check.', on_delta=lambda text: None)
    if not any(c.get('function', {}).get('name') == 'connection_check' for c in result['tool_calls']):
        raise ValueError('Text generation worked, but this model did not call the test tool. Choose a model with tool support.')
    return result.get('_usage', {})


def complete_native(config, history, tools, system, on_delta):
    anthropic = config['api_type'] == 'anthropic'
    wire, names, native_ids = [], {}, set()
    for message in history:
        role = 'assistant' if anthropic else 'model'
        if message['role'] == 'tool':
            if anthropic:
                parts = [dict(type='tool_result', tool_use_id=message['tool_call_id'], content=message['content'])]
            else:
                response = dict(name=names.get(message['tool_call_id'], message['tool_call_id']), response=json.loads(message['content']))
                if message['tool_call_id'] in native_ids: response['id'] = message['tool_call_id']
                parts = [dict(functionResponse=response)]
            role = 'user'
        else:
            if message['role'] != 'assistant': role = 'user'
            native_key = '_anthropic_content' if anthropic else '_gemini_parts'
            parts = copy.deepcopy(message.get(native_key))
            if parts is None:
                parts = ([dict(type='text', text=message['content'])] if anthropic else [dict(text=message['content'])]) if message.get('content') else []
                for image in message.get('_images', []):
                    encoded = image['data_url'].split(',', 1)[1]
                    parts.append(dict(type='image', source=dict(type='base64', media_type=image['mime_type'], data=encoded)) if anthropic else dict(inlineData=dict(mimeType=image['mime_type'], data=encoded)))
                for call in message.get('tool_calls', []):
                    fn = call['function']; args = json.loads(fn['arguments'])
                    parts.append(dict(type='tool_use', id=call['id'], name=fn['name'], input=args) if anthropic else dict(functionCall=dict(name=fn['name'], args=args)))
            for call in message.get('tool_calls', []): names[call['id']] = call['function']['name']
            for part in message.get('_gemini_parts', []):
                if part.get('functionCall', {}).get('id'): native_ids.add(part['functionCall']['id'])
        if not parts: continue
        key = 'content' if anthropic else 'parts'
        if wire and wire[-1]['role'] == role: wire[-1][key].extend(parts)
        else: wire.append(dict(role=role, **{key:parts}))
    if anthropic:
        payload = dict(model=config['model'], system=system, messages=wire, max_tokens=config['max_tokens'], tools=[dict(name=t['name'], description=t['description'], input_schema=t['inputSchema']) for t in tools])
        if not tools: payload.pop('tools')
        if on_delta is not None: payload['stream'] = True
        result = request_provider(config, '/messages', json=payload, on_delta=on_delta)
        if result.get('stop_reason') == 'max_tokens': raise ValueError('The response reached its token limit. Increase the response limit.')
        parts = result.get('content', [])
        calls = [dict(id=p['id'], type='function', function=dict(name=p['name'], arguments=json.dumps(p['input']))) for p in parts if p.get('type') == 'tool_use']
        content = '\n'.join(p['text'] for p in parts if p.get('type') == 'text')
        return dict(role='assistant', content=content, tool_calls=calls, _anthropic_content=parts, _usage=usage(config, result.get('usage')))
    model = quote(config['model'].removeprefix('models/'), safe='')
    payload = dict(systemInstruction=dict(parts=[dict(text=system)]), contents=wire, generationConfig=dict(maxOutputTokens=config['max_tokens']), tools=[dict(functionDeclarations=[dict(name=t['name'], description=t['description'], parametersJsonSchema=t['inputSchema']) for t in tools])])
    if not tools: payload.pop('tools')
    path = f'/models/{model}:' + ('streamGenerateContent?alt=sse' if on_delta is not None else 'generateContent')
    result = request_provider(config, path, json=payload, on_delta=on_delta)
    candidates = result.get('candidates', [])
    if not candidates: raise ValueError('The provider returned no response. Check model or content restrictions.')
    if candidates[0].get('finishReason') == 'MAX_TOKENS': raise ValueError('The response reached its token limit. Increase the response limit.')
    parts = candidates[0].get('content', {}).get('parts', [])
    calls = [dict(id=p['functionCall'].get('id', f'gemini_{index}_{len(history)}'), type='function', function=dict(name=p['functionCall']['name'], arguments=json.dumps(p['functionCall'].get('args', {})))) for index, p in enumerate(parts) if p.get('functionCall')]
    content = '\n'.join(p['text'] for p in parts if p.get('text') and not p.get('thought'))
    return dict(role='assistant', content=content, tool_calls=calls, _gemini_parts=parts, _usage=usage(config, result.get('usageMetadata')))


def transcribe(config, data, filename, on_usage=None):
    result = request_provider(config, '/audio/transcriptions', data={'model': config['speech_model']},
                              files={'file': (filename, data, 'application/octet-stream')})
    if not isinstance(result.get('text'), str) or not result['text'].strip():
        raise ValueError('The speech endpoint did not return a transcript.')
    if on_usage is not None: on_usage(result.get('usage') or {})
    return result['text'][:8000]
