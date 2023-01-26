import os
import re
import math
import subprocess
import concurrent.futures
import pandas as pd
from typing import Optional, Dict

import openai

from mindsdb.integrations.libs.base import BaseMLEngine
from mindsdb.utilities.config import Config
from mindsdb.integrations.handlers.openai_handler.helpers import retry_with_exponential_backoff


class OpenAIHandler(BaseMLEngine):
    name = 'openai'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.default_model = 'text-davinci-002'
        self.rate_limit = 60  # requests per minute
        self.max_batch_size = 20
        self.default_max_tokens = 20

    @staticmethod
    def create_validation(target, args=None, **kwargs):
        if 'using' not in args:
            raise Exception("OpenAI engine requires a USING clause! Refer to its documentation for more details.")
        else:
            args = args['using']

        if 'question_column' not in args and 'prompt_template' not in args:
            raise Exception(f'Either of `question_column` or `prompt_template` are required.')

        if 'prompt_template' in args and 'question_column' in args:
            raise Exception('Please provide either 1) a `prompt_template` or 2) a `question_column` and an optional `context_column`, but not both.')  # noqa

    def create(self, target, args=None, **kwargs):
        args = args['using']

        args['target'] = target
        self.model_storage.json_set('args', args)

    def _get_api_key(self, args):
        # API_KEY preference order:
        #   1. provided at model creation
        #   2. provided at engine creation
        #   3. OPENAI_API_KEY env variable
        #   4. openai.api_key setting in config.json

        # 1
        if 'api_key' in args:
            return args['api_key']
        # 2
        connection_args = self.engine_storage.get_connection_args()
        if 'api_key' in connection_args:
            return connection_args['api_key']
        # 3
        api_key = os.getenv('OPENAI_API_KEY')
        if api_key is not None:
            return api_key
        # 4
        config = Config()
        openai_cfg = config.get('openai', {})
        if 'api_key' in openai_cfg:
            return openai_cfg['api_key']

        raise Exception('Missing API key. Either re-create this ML_ENGINE with your key in the `api_key` parameter,\
             or re-create this model and pass the API key it with `USING` syntax.')  # noqa

    def predict(self, df, args=None):
        """
        If there is a prompt template, we use it. Otherwise, we use the concatenation of `context_column` (optional) and `question_column` to ask for a completion.
        """ # noqa
        # TODO: support for edits, embeddings and moderation

        pred_args = args['predict_params'] if args else {}
        args = self.model_storage.json_get('args')

        if args.get('question_column', False) and args['question_column'] not in df.columns:
            raise Exception(f"This model expects a question to answer in the '{args['question_column']}' column.")

        if args.get('context_column', False) and args['context_column'] not in df.columns:
            raise Exception(f"This model expects context in the '{args['context_column']}' column.")

        model_name = args.get('model_name', self.default_model)
        temperature = min(1.0, max(0.0, args.get('temperature', 0.0)))
        max_tokens = pred_args.get('max_tokens', args.get('max_tokens', self.default_max_tokens))

        if args.get('prompt_template', False):
            if pred_args.get('prompt_template', False):
                base_template = pred_args['prompt_template']  # override with predict-time template if available
            else:
                base_template = args['prompt_template']
            columns = []
            spans = []
            matches = list(re.finditer("{{(.*?)}}", base_template))

            first_span = matches[0].start()
            last_span = matches[-1].end()

            for m in matches:
                columns.append(m[0].replace('{', '').replace('}', ''))
                spans.extend((m.start(), m.end()))

            spans = spans[1:-1]
            template = [base_template[s:e] for s, e in zip(spans, spans[1:])]
            template.insert(0, base_template[0:first_span])
            template.append(base_template[last_span:])

            df['__mdb_prompt'] = ''
            for i in range(len(template)):
                atom = template[i]
                if i < len(columns):
                    col = df[columns[i]]
                    df['__mdb_prompt'] = df['__mdb_prompt'].apply(lambda x: x + atom) + col
                else:
                    df['__mdb_prompt'] = df['__mdb_prompt'].apply(lambda x: x + atom)
            prompts = list(df['__mdb_prompt'])

        elif args.get('context_column', False):
            contexts = list(df[args['context_column']].apply(lambda x: str(x)))
            questions = list(df[args['question_column']].apply(lambda x: str(x)))
            prompts = [f'Context: {c}\nQuestion: {q}\nAnswer: ' for c, q in zip(contexts, questions)]

        else:
            prompts = list(df[args['question_column']].apply(lambda x: str(x)))

        api_key = self._get_api_key(args)
        completion = self._completion(model_name, prompts, max_tokens, temperature, api_key, args)
        pred_df = pd.DataFrame(completion, columns=[args['target']])
        return pred_df

    def _completion(self, model_name, prompts, max_tokens, temperature, api_key, args, parallel=True):
        """
        Handles completion for an arbitrary amount of rows.

        There are a couple checks that should be done when calling OpenAI's API:
          - account max batch size, to maximize batch size first
          - account rate limit, to maximize parallel calls second

        Additionally, single completion calls are done with exponential backoff to guarantee all prompts are processed,
        because even with previous checks the tokens-per-minute limit may apply.
        """
        @retry_with_exponential_backoff
        def _submit_completion(model_name, prompts, max_tokens, temperature, api_key, args):
            return openai.Completion.create(
                model=model_name,
                prompt=prompts,
                max_tokens=max_tokens,
                temperature=temperature,
                api_key=api_key,
                organization=args.get('api_organization')
            )

        def _tidy(comp):
            return [c['text'].strip('\n').strip('') for c in comp['choices']]

        try:
            # check if simple completion works
            completion = _submit_completion(
                model_name,
                prompts,
                max_tokens,
                temperature,
                api_key,
                args
            )
            return _tidy(completion)  
        except openai.error.InvalidRequestError as e:
            # else, we get the max batch size
            e = e.user_message
            if 'you can currently request up to at most a total of' in e:
                pattern = 'a total of'
                max_batch_size = int(e[e.find(pattern) + len(pattern):].split(').')[0])
            else:
                max_batch_size = self.max_batch_size  # guards against changes in the API message

        if not parallel:
            completion = None
            for i in range(math.ceil(len(prompts) / max_batch_size)):
                partial = _submit_completion(model_name,
                                             prompts[i * max_batch_size:(i + 1) * max_batch_size],
                                             max_tokens,
                                             temperature,
                                             api_key,
                                             args)
                if not completion:
                    completion = partial
                else:
                    completion['choices'].extend(partial['choices'])
                    for field in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                        completion['usage'][field] += partial['usage'][field]
        else:
            promises = []
            with concurrent.futures.ThreadPoolExecutor() as executor:
                for i in range(math.ceil(len(prompts) / max_batch_size)):
                    print(f'{i * max_batch_size}:{(i+1) * max_batch_size}/{len(prompts)}')
                    future = executor.submit(_submit_completion,
                                             model_name,
                                             prompts[i * max_batch_size:(i + 1) * max_batch_size],
                                             max_tokens,
                                             temperature,
                                             api_key,
                                             args)
                    promises.append({"choices": future})
            completion = None
            for p in promises:
                if not completion:
                    completion = p['choices'].result()
                else:
                    completion['choices'].extend(p['choices'].result()['choices'])

        return _tidy(completion)

    def describe(self, attribute: Optional[str] = None) -> pd.DataFrame:
        args = self.model_storage.json_get('args')
        api_key = self._get_api_key(args)

        model_name = args.get('model_name', self.default_model)
        meta = openai.Model.retrieve(model_name, api_key=api_key)

        return pd.DataFrame([[meta['id'], meta['object'], meta['owned_by'], meta['permission'], args]],
                            columns=['id', 'object', 'owned_by', 'permission', 'model_args'])

    def update(self, df: Optional[pd.DataFrame] = None, args: Optional[Dict] = None) -> None:
        """
        1. take DF, write to JSONL (where? ask andrey)
        2. use CLI to generate improved splits (again, where? also do we need to install CLI or is pypi enough)
        3. take files, upload using API
        4. send request in
        5. Add to describe (or somehow) the state of each fine-tune... maybe we need to mark as complete only once (with polling) we know it's done?
        """
        if {'prompt', 'completion'} not in set(df.columns):
            raise Exception("To fine-tune an OpenAI model, please format your select data query to have a `prompt` column and a `completion` column first.")  # noqa

        args = self.model_storage.json_get('args')
        openai.api_key = self._get_api_key(args)
        folder_name = 'openai_temp_finetune'

        temp_storage_path = self.engine_storage.folder_get(folder_name)
        temp_file_name = "temp"
        temp_model_storage_path = f"{temp_storage_path}/{temp_file_name}.jsonl"
        df.to_json(temp_model_storage_path, orient='records', lines=True)

        # apply automated OpenAI recommendations to the JSON-lines file
        prepare_result = subprocess.run(
            [
                "openai", "tools", "fine_tunes.prepare_data",
                "-f", temp_model_storage_path,                  # from file
                '-q'                                            # quiet mode (accepts all suggestions)
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )

        returns = []
        for file_name in [f'{temp_file_name}_train.jsonl', f'{temp_file_name}_valid.jsonl']:
            returns.append(openai.File.create(
                file=open(f"{temp_storage_path}/{file_name}", "rb"),
                purpose='fine-tune')
            )

        # all `None` values are left unspecified and internally imputed by OpenAI to `null` or default values
        ft_params = {
            'training_file': returns[0].id,
            'validation_file': returns[1].id,
            'model': 'ada',                         # one of 'ada', 'curie', 'davinci', 'babbage'
            'suffix': 'mindsdb',
            'n_epochs': None,
            'batch_size': None,
            'learning_rate_multiplier': None,
            'prompt_loss_weight': None,
            'compute_classification_metrics': None,
            'classification_n_classes': None,
            'classification_positive_class': None,
            'classification_betas': None,
        }

        # TODO: move this into a detached learn process
        ft_result = openai.FineTune.create(**{k: v for k, v in ft_params.items() if v is not None})
        ft_retrieved = openai.FineTune.retrieve(id=ft_result.id)  # TODO check 'pending' initial status until 'succeeded'  # noqa

        ft_model_name = ft_retrieved['fine_tuned_model']
        result_file_id = openai.FineTune.retrieve(id=ft_result.id)['result_files'][0].id

        name_extension = openai.File.retrieve(id=result_file_id).filename
        result_path = f'{temp_storage_path}/ft_result_{name_extension}'
        with open(result_path, 'wb') as f:
            f.write(openai.File.download(id=result_file_id))

        # TODO: check this only if classification framing detected
        ft_stats = pd.read_csv(result_path)
        val_loss = ft_stats[ft_stats['validation_token_accuracy'].notnull()]

        self.engine_storage.folder_sync(folder_name)

        # To use the fine-tuned model:
        # res = openai.Completion.create(model=ft_model_name, prompt='this is a prompt' + '\n\n###\n\n', max_tokens=1,
        #                                temperature=0)
        # res['choices'][0]['text']
