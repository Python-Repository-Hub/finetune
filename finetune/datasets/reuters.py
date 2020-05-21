import os
import requests
import codecs
import json
import hashlib
import io
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup as bs
from bs4.element import Tag
from sklearn.model_selection import train_test_split

from finetune import SequenceLabeler
from finetune.datasets import Dataset
from finetune.base_models import GPT, GPT2, TCN, RoBERTa
from finetune.encoding.sequence_encoder import finetune_to_indico_sequence
from finetune.util.metrics import annotation_report, sequence_labeling_token_confusion

XML_PATH = os.path.join("Data", "Sequence", "reuters.xml")
DATA_PATH = os.path.join("Data", "Sequence", "reuters.json")
CHECKSUM = "a79cab99ed30b7932d46711ef8d662e0"

class Reuters(Dataset):

    def __init__(self, filename=None, **kwargs):
        super().__init__(filename=(filename or DATA_PATH), **kwargs)

    @property
    def md5(self):
        return CHECKSUM

    def download(self):

        url = "https://raw.githubusercontent.com/dice-group/n3-collection/master/reuters.xml"
        r = requests.get(url)

        with open(XML_PATH, 'wb') as fd:
            fd.write(r.content)

        fd = open(XML_PATH)
        soup = bs(fd, "html.parser")
        docs = []
        docs_labels = []
        for elem in soup.find_all("document"):
            texts = []
            labels = []

            # Loop through each child of the element under "textwithnamedentities"
            for c in elem.find("textwithnamedentities").children:
                if type(c) == Tag:
                    if c.name == "namedentityintext":
                        label = "Named Entity"  # part of a named entity
                    else:
                        label = "<PAD>"  # irrelevant word
                    texts.append(c.text)
                    labels.append(label)

            docs.append(texts)
            docs_labels.append(labels)

        fd.close()
        os.remove(XML_PATH)

        raw_texts = ["".join(doc) for doc in docs]
        texts, annotations = finetune_to_indico_sequence(raw_texts, docs, docs_labels, none_value="<PAD>",
                                                         subtoken_predictions=True)
        df = pd.DataFrame({'texts': texts, 'annotations': [json.dumps(annotation) for annotation in annotations]})
        df.to_csv(DATA_PATH)


if __name__ == "__main__":

    from transformers import *
    from transformers.modeling_tf_electra import TFElectraMainLayer
    from finetune.util.huggingface_interface import finetune_model_from_huggingface
    
    """
    pretrained_weights = "google/electra-base-generator"
    base_model = finetune_model_from_huggingface(
        weights_url=TF_ELECTRA_PRETRAINED_MODEL_ARCHIVE_MAP[pretrained_weights],
        hf_featurizer=TFElectraMainLayer,
        hf_tokenizer=ElectraTokenizerFast.from_pretrained(pretrained_weights),
        hf_config=ElectraConfig.from_pretrained(pretrained_weights),
        weights_replacement=[
            ("tf_bert_for_pre_training_2/bert/", "model/featurizer/tf_bert_main_layer/"),
            ("tf_electra_for_masked_lm/electra", "model/featurizer/tf_electra_main_layer")
        ]
    )
    """
    pretrained_weights = "transfo-xl-wt103"
    base_model = finetune_model_from_huggingface(
        weights_url=TF_TRANSFO_XL_PRETRAINED_MODEL_ARCHIVE_MAP[pretrained_weights],
        hf_featurizer=TFTransfoXLMainLayer,
        hf_tokenizer=TransfoXLTokenizerFast.from_pretrained(pretrained_weights),
        hf_config=TransfoXLConfig.from_pretrained(pretrained_weights),
        weights_replacement=[
            ("tf_bert_for_pre_training_2/bert/", "model/featurizer/tf_bert_main_layer/"),
            ("tf_electra_for_masked_lm/electra", "model/featurizer/tf_electra_main_layer")
        ]
    )
    dataset = Reuters().dataframe
    dataset['annotations'] = [json.loads(annotation) for annotation in dataset['annotations']]
    trainX, testX, trainY, testY = train_test_split(
        dataset.texts.values,
        dataset.annotations.values,
        test_size=0.2,
        random_state=42
    )
    model = SequenceLabeler(base_model=base_model, n_epochs=3)
    model.fit(trainX, trainY)
    predictions = model.predict(testX)
    print(predictions)
    print(annotation_report(testY, predictions))
    sequence_labeling_token_confusion(testX, testY, predictions)
