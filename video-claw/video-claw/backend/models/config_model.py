"""Central model registry.

All backend model metadata is registered in this file, including provider,
model type, concurrency, pricing, and API capability tags.
"""

import logging
import os
import sys

models_dir = os.path.dirname(os.path.abspath(__file__))
backend_dir = os.path.dirname(models_dir)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from copy import deepcopy
from typing import Any, Optional

logger = logging.getLogger(__name__)

MODEL_CONFIG: dict[str, Any] = {'models': {'deepseek-chat': {'name': 'DeepSeek Chat',
                              'provider': 'deepseek',
                              'type': ['llm'],
                              'concurrency': 10,
                              'price_per_1k_input_token': 0.002,
                              'price_per_1k_output_token': 0.008},
            'deepseek-reasoner': {'name': 'DeepSeek Reasoner',
                                  'provider': 'deepseek',
                                  'type': ['llm'],
                                  'concurrency': 10,
                                  'price_per_1k_input_token': 0.004,
                                  'price_per_1k_output_token': 0.016},
            'deepseek-v4-flash': {'name': 'DeepSeek V4 Flash',
                                  'provider': 'deepseek',
                                  'type': ['llm'],
                                  'concurrency': 10,
                                  'price_per_1k_input_token': 0.001,
                                  'price_per_1k_output_token': 0.004},
            'deepseek-v4-pro': {'name': 'DeepSeek V4 Pro',
                                'provider': 'deepseek',
                                'type': ['llm'],
                                'concurrency': 10,
                                'price_per_1k_input_token': 0.002,
                                'price_per_1k_output_token': 0.008},
            'gpt-4o': {'name': 'GPT-4o',
                       'provider': 'openai',
                       'type': ['llm'],
                       'concurrency': 10,
                       'price_per_1k_input_token': 0.018,
                       'price_per_1k_output_token': 0.072},
            'gpt-5': {'name': 'GPT-5',
                      'provider': 'openai',
                      'type': ['llm'],
                      'concurrency': 10,
                      'price_per_1k_input_token': 0.02,
                      'price_per_1k_output_token': 0.1},
            'gpt-5.4': {'name': 'GPT-5.4',
                        'provider': 'openai',
                        'type': ['llm', 'vlm'],
                        'concurrency': 10,
                        'price_per_1k_input_token': 0.03,
                        'price_per_1k_output_token': 0.12},
            'qwen3.7-max': {'name': 'Qwen 3.7 Max',
                            'provider': 'dashscope',
                            'family': 'qwen',
                            'type': ['llm'],
                            'concurrency': 10,
                            'price_per_1k_input_token': 0.006,
                            'price_per_1k_output_token': 0.024},
            'qwen3.7-plus': {'name': 'Qwen 3.7 Plus',
                             'provider': 'dashscope',
                             'family': 'qwen',
                             'type': ['vlm'],
                             'concurrency': 10,
                             'price_per_1k_input_token': 0.0008,
                             'price_per_1k_output_token': 0.002},
            'qwen3.5-plus': {'name': 'Qwen 3.5 Plus',
                             'provider': 'dashscope',
                             'family': 'qwen',
                             'type': ['vlm'],
                             'concurrency': 10,
                             'price_per_1k_input_token': 0.0008,
                             'price_per_1k_output_token': 0.002},
            'qwen3.6-max-preview': {'name': 'Qwen 3.6 Max Preview',
                                    'provider': 'dashscope',
                                    'family': 'qwen',
                                    'type': ['llm'],
                                    'concurrency': 10,
                                    'price_per_1k_input_token': 0.006,
                                    'price_per_1k_output_token': 0.024},
            'qwen3-max': {'name': 'Qwen 3 Max',
                          'provider': 'dashscope',
                          'family': 'qwen',
                          'type': ['llm'],
                          'concurrency': 10,
                          'price_per_1k_input_token': 0.006,
                          'price_per_1k_output_token': 0.024},
            'deepseek-v3.2': {'name': 'DeepSeek V3.2 (DashScope)',
                              'provider': 'dashscope',
                              'family': 'deepseek',
                              'type': ['llm'],
                              'concurrency': 10,
                              'price_per_1k_input_token': 0.002,
                              'price_per_1k_output_token': 0.008},
            'qwen3.6-plus': {'name': 'Qwen 3.6 Plus',
                             'provider': 'dashscope',
                             'family': 'qwen',
                             'type': ['vlm'],
                             'concurrency': 10,
                             'price_per_1k_input_token': 0.0008,
                             'price_per_1k_output_token': 0.002},
            'qwen3.6-flash': {'name': 'Qwen 3.6 Flash',
                              'provider': 'dashscope',
                              'family': 'qwen',
                              'type': ['vlm'],
                              'concurrency': 10,
                              'price_per_1k_input_token': 0.0001,
                              'price_per_1k_output_token': 0.0001},
            'kimi-k2.6': {'name': 'Kimi K2.6',
                          'provider': 'dashscope',
                          'family': 'kimi',
                          'type': ['llm', 'vlm'],
                          'concurrency': 10,
                          'price_per_1k_input_token': 0.001,
                          'price_per_1k_output_token': 0.003},
            'gemini-2.5-flash': {'name': 'Gemini 2.5 Flash',
                                 'provider': 'gemini',
                                 'type': ['llm'],
                                 'concurrency': 10,
                                 'price_per_1k_input_token': 0.002,
                                 'price_per_1k_output_token': 0.01},
            'gemini-2.0-flash': {'name': 'Gemini 2.0 Flash',
                                 'provider': 'gemini',
                                 'type': ['llm', 'vlm'],
                                 'concurrency': 10,
                                 'price_per_1k_input_token': 0.002,
                                 'price_per_1k_output_token': 0.01},
            'gemini-2.5-flash-image': {'name': 'Gemini 2.5 Flash Image',
                                       'provider': 'gemini',
                                       'type': ['vlm'],
                                       'concurrency': 10,
                                       'price_per_1k_input_token': 0.002,
                                       'price_per_1k_output_token': 0.01},
            'wan2.7-image': {'name': 'Wan 2.7 Image',
                             'provider': 'dashscope',
                             'family': 'wan',
                             'type': ['t2i', 'i2i'],
                             'concurrency': 5,
                             'price_per_image': 0.2,
                             'capabilities': {'ability_type': 'image_generation',
                                              'ability_types': ['text_to_image', 'image_to_image', 'reference_image'],
                                              'adapter_ability_types': ['text_to_image',
                                                                        'image_to_image',
                                                                        'reference_image'],
                                              'input_modalities': ['text', 'image'],
                                              'adapter_input_modalities': ['text', 'image'],
                                              'api_contract_verified': True,
                                              'resolutions': ['720P', '1080P', '2K', '4K'],
                                              'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4']}},
            'wan2.7-image-pro': {'name': 'Wan 2.7 Image Pro',
                                 'provider': 'dashscope',
                                 'family': 'wan',
                                 'type': ['t2i', 'i2i'],
                                 'concurrency': 5,
                                 'price_per_image': 0.4,
                                 'capabilities': {'ability_type': 'image_generation',
                                                  'ability_types': ['text_to_image',
                                                                    'image_to_image',
                                                                    'reference_image',
                                                                    'high_quality'],
                                                  'adapter_ability_types': ['text_to_image',
                                                                            'image_to_image',
                                                                            'reference_image'],
                                                  'input_modalities': ['text', 'image'],
                                                  'adapter_input_modalities': ['text', 'image'],
                                                  'api_contract_verified': True,
                                                  'resolutions': ['720P', '1080P', '2K', '4K'],
                                                  'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4']}},
            'wan2.6-t2i': {'name': 'Wan 2.6 T2I',
                           'provider': 'dashscope',
                           'family': 'wan',
                           'type': ['t2i'],
                           'concurrency': 5,
                           'price_per_image': 0.2,
                           'capabilities': {'ability_type': 'image_generation',
                                            'ability_types': ['text_to_image'],
                                            'adapter_ability_types': ['text_to_image'],
                                            'input_modalities': ['text'],
                                            'adapter_input_modalities': ['text'],
                                            'api_contract_verified': True,
                                            'resolutions': ['720P', '1080P', '2K', '4K'],
                                            'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4']}},
            'doubao-seedream-5-0-260128': {'name': 'Seedream 5.0',
                                           'provider': 'ark',
                                           'family': 'seedream',
                                           'type': ['t2i', 'i2i'],
                                           'concurrency': 10,
                                           'price_per_image': 0.22,
                                           'capabilities': {'ability_type': 'image_generation',
                                                            'ability_types': ['text_to_image',
                                                                              'image_to_image',
                                                                              'reference_image',
                                                                              'high_quality'],
                                                            'adapter_ability_types': ['text_to_image',
                                                                                      'image_to_image',
                                                                                      'reference_image'],
                                                            'input_modalities': ['text', 'image'],
                                                            'adapter_input_modalities': ['text', 'image'],
                                                            'api_contract_verified': True,
                                                            'resolutions': ['720P', '1080P', '2K', '4K'],
                                                            'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4']}},
            'doubao-seedream-4-5-251128': {'name': 'Seedream 4.5',
                                           'provider': 'ark',
                                           'family': 'seedream',
                                           'type': ['t2i', 'i2i'],
                                           'concurrency': 10,
                                           'price_per_image': 0.25,
                                           'capabilities': {'ability_type': 'image_generation',
                                                            'ability_types': ['text_to_image',
                                                                              'image_to_image',
                                                                              'reference_image'],
                                                            'adapter_ability_types': ['text_to_image',
                                                                                      'image_to_image',
                                                                                      'reference_image'],
                                                            'input_modalities': ['text', 'image'],
                                                            'adapter_input_modalities': ['text', 'image'],
                                                            'api_contract_verified': True,
                                                            'resolutions': ['720P', '1080P', '2K', '4K'],
                                                            'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4']}},
            'doubao-seedream-4-0-250828': {'name': 'Seedream 4.0',
                                           'provider': 'ark',
                                           'family': 'seedream',
                                           'type': ['t2i', 'i2i'],
                                           'concurrency': 10,
                                           'price_per_image': 0.2,
                                           'capabilities': {'ability_type': 'image_generation',
                                                            'ability_types': ['text_to_image',
                                                                              'image_to_image',
                                                                              'reference_image'],
                                                            'adapter_ability_types': ['text_to_image',
                                                                                      'image_to_image',
                                                                                      'reference_image'],
                                                            'input_modalities': ['text', 'image'],
                                                            'adapter_input_modalities': ['text', 'image'],
                                                            'api_contract_verified': True,
                                                            'resolutions': ['720P', '1080P', '2K', '4K'],
                                                            'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4']}},
            'sora_image': {'name': 'Sora Image',
                           'provider': 'openai',
                           'type': ['t2i'],
                           'concurrency': 3,
                           'price_per_image': 1.45,
                           'capabilities': {'ability_type': 'image_generation',
                                            'ability_types': ['text_to_image'],
                                            'adapter_ability_types': ['text_to_image'],
                                            'input_modalities': ['text'],
                                            'adapter_input_modalities': ['text'],
                                            'api_contract_verified': True}},
            'gpt-image-2': {'name': 'GPT Image 2',
                            'provider': 'openai',
                            'type': ['t2i', 'i2i'],
                            'concurrency': 3,
                            'price_per_image': 1.09,
                            'capabilities': {'ability_type': 'image_generation',
                                             'ability_types': ['text_to_image', 'image_to_image', 'reference_image'],
                                             'adapter_ability_types': ['text_to_image',
                                                                       'image_to_image',
                                                                       'reference_image'],
                                             'input_modalities': ['text', 'image'],
                                             'adapter_input_modalities': ['text', 'image'],
                                             'api_contract_verified': True}},
            'wan2.6-i2v-flash': {'name': 'Wan 2.6 I2V Flash',
                                 'provider': 'dashscope',
                                 'family': 'wan',
                                 'type': ['video'],
                                 'concurrency': 5,
                                 'price_per_second': 0.6,
                                 'capabilities': {'ability_type': 'image_to_video',
                                                  'ability_types': ['first_frame_i2v',
                                                                    'audio_driven_i2v',
                                                                    'multi_shot',
                                                                    'fast_generation'],
                                                  'adapter_ability_types': ['first_frame_i2v'],
                                                  'input_modalities': ['text', 'image', 'audio'],
                                                  'adapter_input_modalities': ['text', 'image'],
                                                  'duration': {'min': 2, 'max': 15, 'integer': True, 'verified': True},
                                                  'resolutions': ['720P', '1080P'],
                                                  'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4'],
                                                  'api_contract_verified': True}},
            'wan2.7-i2v': {'name': 'Wan 2.7 I2V',
                           'provider': 'dashscope',
                           'family': 'wan',
                           'type': ['video'],
                           'concurrency': 5,
                           'price_per_second': 0.8,
                           'capabilities': {'ability_type': 'image_to_video',
                                            'ability_types': ['first_frame_i2v',
                                                              'start_end_frame_i2v',
                                                              'video_continuation',
                                                              'audio_driven_i2v',
                                                              'multi_shot'],
                                            'adapter_ability_types': ['first_frame_i2v',
                                                                      'start_end_frame_i2v',
                                                                      'audio_driven_i2v'],
                                            'input_modalities': ['text', 'image', 'audio', 'video'],
                                            'adapter_input_modalities': ['text', 'image'],
                                            'duration': {'min': 2, 'max': 15, 'integer': True, 'verified': True},
                                            'resolutions': ['720P', '1080P'],
                                            'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4'],
                                            'api_contract_verified': True}},
            'happyhorse-1.0-i2v': {'name': 'Happy Horse 1.0 I2V',
                                   'provider': 'dashscope',
                                   'family': 'happyhorse',
                                   'type': ['video'],
                                   'concurrency': 5,
                                   'price_per_second': 1.0,
                                   'capabilities': {'ability_type': 'image_to_video',
                                                    'ability_types': ['first_frame_i2v', 'native_audio'],
                                                    'adapter_ability_types': ['first_frame_i2v', 'native_audio'],
                                                    'input_modalities': ['text', 'image'],
                                                    'adapter_input_modalities': ['text', 'image'],
                                                    'duration': {'min': 3,
                                                                 'max': 15,
                                                                 'integer': True,
                                                                 'verified': True},
                                                    'resolutions': ['720P', '1080P'],
                                                    'api_contract_verified': True}},
            'happyhorse-1.1-i2v': {'name': 'Happy Horse 1.1 I2V',
                                   'provider': 'dashscope',
                                   'family': 'happyhorse',
                                   'type': ['video'],
                                   'concurrency': 5,
                                   'price_per_second': 1.0,
                                   'capabilities': {'ability_type': 'image_to_video',
                                                    'ability_types': ['first_frame_i2v', 'native_audio'],
                                                    'adapter_ability_types': ['first_frame_i2v', 'native_audio'],
                                                    'input_modalities': ['text', 'image'],
                                                    'adapter_input_modalities': ['text', 'image'],
                                                    'duration': {'min': 3,
                                                                 'max': 15,
                                                                 'integer': True,
                                                                 'verified': True},
                                                    'resolutions': ['720P', '1080P'],
                                                    'api_contract_verified': True}},
            'kling-v3': {'name': 'Kling V3',
                         'provider': 'kling',
                         'type': ['video'],
                         'concurrency': 10,
                         'price_per_second': 1.0,
                         'capabilities': {'ability_type': 'image_to_video',
                                          'ability_types': ['text_to_video',
                                                            'image_to_video',
                                                            'start_end_frame_i2v',
                                                            'native_audio',
                                                            'multi_shot',
                                                            'element_reference'],
                                          'adapter_ability_types': ['first_frame_i2v', 'native_audio'],
                                          'input_modalities': ['text', 'image'],
                                          'adapter_input_modalities': ['text', 'image'],
                                          'duration': {'min': 3, 'max': 15, 'integer': True, 'verified': True},
                                          'resolutions': ['720P', '1080P'],
                                          'api_contract_verified': True}},
            'kling-v2-6': {'name': 'Kling V2.6',
                           'provider': 'kling',
                           'type': ['video'],
                           'concurrency': 10,
                           'price_per_second': 0.5,
                           'capabilities': {'ability_type': 'image_to_video',
                                            'ability_types': ['text_to_video',
                                                              'image_to_video',
                                                              'start_end_frame_i2v',
                                                              'native_audio'],
                                            'adapter_ability_types': ['first_frame_i2v', 'native_audio'],
                                            'input_modalities': ['text', 'image'],
                                            'adapter_input_modalities': ['text', 'image'],
                                            'api_contract_verified': True}},
            'kling-v2-5-turbo': {'name': 'Kling V2.5 Turbo',
                                 'provider': 'kling',
                                 'type': ['video'],
                                 'concurrency': 10,
                                 'price_per_second': 0.3,
                                 'capabilities': {'ability_type': 'image_to_video',
                                                  'ability_types': ['image_to_video'],
                                                  'adapter_ability_types': ['first_frame_i2v', 'native_audio'],
                                                  'input_modalities': ['text', 'image'],
                                                  'adapter_input_modalities': ['text', 'image'],
                                                  'api_contract_verified': True}},
            'doubao-seedance-2-0-260128': {'name': 'Seedance 2.0',
                                           'provider': 'ark',
                                           'family': 'seedance',
                                           'type': ['video'],
                                           'concurrency': 10,
                                           'price_per_second': 0.5,
                                           'capabilities': {'ability_type': 'image_to_video',
                                                            'ability_types': ['text_to_video', 'image_to_video'],
                                                            'adapter_ability_types': ['first_frame_i2v',
                                                                                      'native_audio'],
                                                            'input_modalities': ['text', 'image'],
                                                            'adapter_input_modalities': ['text', 'image'],
                                                            'duration': {'min': 2,
                                                                         'max': 12,
                                                                         'integer': True,
                                                                         'verified': True},
                                                            'resolutions': ['720p', '1080p'],
                                                            'ratios': ['16:9',
                                                                       '4:3',
                                                                       '1:1',
                                                                       '3:4',
                                                                       '9:16',
                                                                       '21:9',
                                                                       'adaptive'],
                                                            'api_contract_verified': True}},
            'doubao-seedance-2-0-fast-260128': {'name': 'Seedance 2.0 Fast',
                                                'provider': 'ark',
                                                'family': 'seedance',
                                                'type': ['video'],
                                                'concurrency': 10,
                                                'price_per_second': 0.3,
                                                'capabilities': {'ability_type': 'image_to_video',
                                                                 'ability_types': ['text_to_video',
                                                                                   'image_to_video',
                                                                                   'fast_generation'],
                                                                 'adapter_ability_types': ['first_frame_i2v',
                                                                                           'native_audio'],
                                                                 'input_modalities': ['text', 'image'],
                                                                 'adapter_input_modalities': ['text', 'image'],
                                                                 'duration': {'min': 2,
                                                                              'max': 12,
                                                                              'integer': True,
                                                                              'verified': True},
                                                                 'resolutions': ['720p', '1080p'],
                                                                 'ratios': ['16:9',
                                                                            '4:3',
                                                                            '1:1',
                                                                            '3:4',
                                                                            '9:16',
                                                                            '21:9',
                                                                            'adaptive'],
                                                                 'api_contract_verified': True}},
            'doubao-seedance-2-0-mini-260615': {'name': 'Seedance 2.0 Mini',
                                                'provider': 'ark',
                                                'family': 'seedance',
                                                'type': ['video'],
                                                'concurrency': 10,
                                                'price_per_second': 0.2,
                                                'capabilities': {'ability_type': 'image_to_video',
                                                                 'ability_types': ['text_to_video',
                                                                                   'image_to_video',
                                                                                   'fast_generation'],
                                                                 'adapter_ability_types': ['first_frame_i2v',
                                                                                           'native_audio'],
                                                                 'input_modalities': ['text', 'image'],
                                                                 'adapter_input_modalities': ['text', 'image'],
                                                                 'duration': {'min': 2,
                                                                              'max': 12,
                                                                              'integer': True,
                                                                              'verified': True},
                                                                 'resolutions': ['720p', '1080p'],
                                                                 'ratios': ['16:9',
                                                                            '4:3',
                                                                            '1:1',
                                                                            '3:4',
                                                                            '9:16',
                                                                            '21:9',
                                                                            'adaptive'],
                                                                 'api_contract_verified': True}},
            'wan2.7-r2v': {'name': 'Wan 2.7 R2V',
                           'provider': 'dashscope',
                           'family': 'wan',
                           'type': ['video'],
                           'concurrency': 5,
                           'price_per_second': 0.8,
                           'capabilities': {'ability_type': 'reference_to_video',
                                            'ability_types': ['reference_to_video',
                                                              'digital_human',
                                                              'multi_character',
                                                              'native_audio',
                                                              'voice_reference',
                                                              'multi_shot'],
                                            'adapter_ability_types': ['reference_to_video',
                                                                      'digital_human',
                                                                      'voice_reference'],
                                            'input_modalities': ['text', 'image', 'audio', 'video'],
                                            'adapter_input_modalities': ['text', 'image', 'audio'],
                                            'duration': {'min': 2, 'max': 10, 'integer': True, 'verified': True},
                                            'resolutions': ['720P', '1080P'],
                                            'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4'],
                                            'api_contract_verified': True}},
            'wan2.7-videoedit': {'name': 'Wan 2.7 Video Edit',
                                 'provider': 'dashscope',
                                 'family': 'wan',
                                 'type': ['video'],
                                 'concurrency': 5,
                                 'price_per_second': 0.8,
                                 'capabilities': {'ability_type': 'video_editing',
                                                  'ability_types': ['video_editing',
                                                                    'action_transfer',
                                                                    'instruction_editing',
                                                                    'video_transfer'],
                                                  'adapter_ability_types': ['action_transfer', 'video_editing'],
                                                  'input_modalities': ['text', 'image', 'video'],
                                                  'adapter_input_modalities': ['text', 'image', 'video'],
                                                  'duration': {'min': 2, 'max': 10, 'integer': True, 'verified': True},
                                                  'resolutions': ['720P', '1080P'],
                                                  'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4'],
                                                  'api_contract_verified': True}},
            'happyhorse-1.0-r2v': {'name': 'Happy Horse 1.0 R2V',
                                   'provider': 'dashscope',
                                   'family': 'happyhorse',
                                   'type': ['video'],
                                   'concurrency': 5,
                                   'price_per_second': 1.0,
                                   'capabilities': {'ability_type': 'reference_to_video',
                                                    'ability_types': ['reference_to_video',
                                                                      'digital_human',
                                                                      'multi_character',
                                                                      'native_audio',
                                                                      'multi_shot'],
                                                    'adapter_ability_types': ['reference_to_video', 'digital_human'],
                                                    'input_modalities': ['text', 'image'],
                                                    'adapter_input_modalities': ['text', 'image'],
                                                    'duration': {'min': 3,
                                                                 'max': 15,
                                                                 'integer': True,
                                                                 'verified': True},
                                                    'resolutions': ['720P', '1080P'],
                                                    'ratios': ['16:9', '9:16', '3:4', '4:3', '1:1'],
                                                    'api_contract_verified': True}},
            'happyhorse-1.1-r2v': {'name': 'Happy Horse 1.1 R2V',
                                   'provider': 'dashscope',
                                   'family': 'happyhorse',
                                   'type': ['video'],
                                   'concurrency': 5,
                                   'price_per_second': 1.0,
                                   'capabilities': {'ability_type': 'reference_to_video',
                                                    'ability_types': ['reference_to_video',
                                                                      'digital_human',
                                                                      'multi_character',
                                                                      'native_audio',
                                                                      'multi_shot'],
                                                    'adapter_ability_types': ['reference_to_video', 'digital_human'],
                                                    'input_modalities': ['text', 'image'],
                                                    'adapter_input_modalities': ['text', 'image'],
                                                    'duration': {'min': 3,
                                                                 'max': 15,
                                                                 'integer': True,
                                                                 'verified': True},
                                                    'resolutions': ['720P', '1080P'],
                                                    'ratios': ['16:9', '9:16', '3:4', '4:3', '1:1'],
                                                    'api_contract_verified': True}},
            'happyhorse-1.0-video-edit': {'name': 'Happy Horse 1.0 Video Edit',
                                          'provider': 'dashscope',
                                          'family': 'happyhorse',
                                          'type': ['video'],
                                          'concurrency': 5,
                                          'price_per_second': 1.0,
                                          'capabilities': {'ability_type': 'video_editing',
                                                           'ability_types': ['video_editing',
                                                                             'action_transfer',
                                                                             'instruction_editing',
                                                                             'native_audio'],
                                                           'adapter_ability_types': ['action_transfer',
                                                                                     'video_editing'],
                                                           'input_modalities': ['text', 'image', 'video'],
                                                           'adapter_input_modalities': ['text', 'image', 'video'],
                                                           'duration': {'min': 3,
                                                                        'max': 15,
                                                                        'integer': True,
                                                                        'verified': True},
                                                           'resolutions': ['720P', '1080P'],
                                                           'api_contract_verified': True}},
            'seedance-1-0-pro': {'name': 'Seedance 1.0 Pro',
                                 'provider': 'ark',
                                 'family': 'seedance',
                                 'type': ['video'],
                                 'concurrency': 10,
                                 'price_per_second': 0.5,
                                 'capabilities': {'ability_type': 'image_to_video',
                                                  'ability_types': [],
                                                  'adapter_ability_types': ['first_frame_i2v'],
                                                  'api_contract_verified': False}},
            'seedance-1-0-lite': {'name': 'Seedance 1.0 Lite',
                                  'provider': 'ark',
                                  'family': 'seedance',
                                  'type': ['video'],
                                  'concurrency': 10,
                                  'price_per_second': 0.3,
                                  'capabilities': {'ability_type': 'image_to_video',
                                                   'ability_types': [],
                                                   'adapter_ability_types': ['first_frame_i2v'],
                                                   'api_contract_verified': False}}}}


# 自定义模型并发缺省值：本地单卡服务最保守，可显式调高
CUSTOM_MODEL_DEFAULT_CONCURRENCY = 1

# 内置供应商 → 凭据字段映射（阶段预检使用）
PROVIDER_CREDENTIAL_FIELDS = {
    "dashscope": "DASHSCOPE_API_KEY",
    "ark": "ARK_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "kling": "KLING_API_KEY",
}


def _builtin_models() -> dict[str, Any]:
    return MODEL_CONFIG.get("models", {})


def _deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _normalize_video_capabilities(capability: dict[str, Any]) -> dict[str, Any]:
    """归一化视频能力声明：duration{min,max}、fps[]、short_edge、ratios[]、resolutions[]。

    用户声明经 _deep_merge_dict 覆盖默认值后调用；非法声明剔除（回落默认/移除），
    保证 /api/models 下发的能力结构始终可被适配层与前端消费。
    """
    # duration：min/max 取整，min<=max；非法时回落默认 2-10
    duration = capability.get("duration")
    if isinstance(duration, dict):
        try:
            minimum = int(duration.get("min", 2))
        except (TypeError, ValueError):
            minimum = 2
        try:
            maximum = int(duration.get("max", 10))
        except (TypeError, ValueError):
            maximum = 10
        if maximum < minimum:
            maximum = minimum
        duration["min"] = minimum
        duration["max"] = maximum
        duration.setdefault("integer", True)
    elif duration is not None:
        capability["duration"] = {"min": 2, "max": 10, "integer": True, "verified": True}
    # fps：归一为去重排序的 int 列表；声明无效/为空则移除（表示"未声明，不注入"）
    fps = capability.get("fps")
    if fps is not None:
        if isinstance(fps, (int, float)):
            fps = [fps]
        if isinstance(fps, (list, tuple)):
            values = []
            for value in fps:
                try:
                    values.append(int(value))
                except (TypeError, ValueError):
                    continue
            if values:
                capability["fps"] = sorted(set(values))
            else:
                capability.pop("fps", None)
        else:
            capability.pop("fps", None)
    # short_edge：单个 int；非法则移除
    short_edge = capability.get("short_edge")
    if short_edge is not None:
        try:
            capability["short_edge"] = int(short_edge)
        except (TypeError, ValueError):
            capability.pop("short_edge", None)
    # ratios / resolutions：归一为字符串列表
    for key in ("ratios", "resolutions"):
        values = capability.get(key)
        if values is not None and isinstance(values, (list, tuple)):
            capability[key] = [str(value) for value in values if str(value).strip()]
    return capability


def _custom_model_capabilities(item: dict[str, Any], types: list[str]) -> dict[str, Any]:
    """按 types/abilities 推导自定义模型能力标签，用户声明的 capabilities 可覆盖默认值。"""
    ability_types: list[str] = []
    input_modalities: list[str] = ["text"]
    if "llm" in types:
        ability_types.append("text_generation")
    if "vlm" in types:
        ability_types.extend(["vision_language", "image_understanding"])
        input_modalities.append("image")
    if "t2i" in types:
        ability_types.append("text_to_image")
    if "i2i" in types:
        ability_types.extend(["image_to_image", "reference_image"])
        input_modalities.append("image")
    if "video" in types:
        declared = [a for a in (item.get("abilities") or []) if a]
        video_abilities = declared or ["text_to_video", "first_frame_i2v"]
        ability_types.extend(video_abilities)
        if any(
            a in ("first_frame_i2v", "start_end_frame_i2v", "reference_to_video", "image_to_video")
            for a in video_abilities
        ):
            input_modalities.append("image")
        # 媒体参考能力标签（vllm-omni MiniMax-H3 ref2va）：声明后纳入输入模态，
        # 供 /api/models ability 过滤与沙盒/工作流入口使用
        if "audio_reference" in video_abilities:
            input_modalities.append("audio")
        if "video_reference" in video_abilities:
            input_modalities.append("video")

    ability_types = list(dict.fromkeys(ability_types))
    input_modalities = list(dict.fromkeys(input_modalities))

    if "video" in types:
        ability_type = "image_to_video" if "first_frame_i2v" in ability_types else "text_to_video"
        # 默认能力：duration 2-10（H3 类模型建议声明 4-15）、fps/short_edge 默认不声明
        # （声明 fps: [24]、short_edge: 768 后适配层才注入对应字段，见 custom_video 映射层）
        capability: dict[str, Any] = {
            "ability_type": ability_type,
            "ability_types": ability_types,
            "adapter_ability_types": ability_types,
            "input_modalities": input_modalities,
            "adapter_input_modalities": input_modalities,
            "duration": {"min": 2, "max": 10, "integer": True, "verified": True},
            "resolutions": ["720P", "1080P"],
            "ratios": ["16:9", "9:16", "1:1", "4:3", "3:4"],
        }
    elif any(t in types for t in ("t2i", "i2i")):
        capability = {
            "ability_type": "image_generation",
            "ability_types": ability_types,
            "adapter_ability_types": ability_types,
            "input_modalities": input_modalities,
            "adapter_input_modalities": input_modalities,
            "resolutions": ["720P", "1080P"],
            "ratios": ["16:9", "9:16", "1:1", "4:3", "3:4"],
        }
    elif "vlm" in types:
        capability = {
            "ability_type": "vision_language",
            "ability_types": ability_types,
            "adapter_ability_types": ability_types,
            "input_modalities": input_modalities,
            "adapter_input_modalities": input_modalities,
        }
    else:
        capability = {
            "ability_type": "text_generation",
            "ability_types": ability_types,
            "adapter_ability_types": ability_types,
            "input_modalities": input_modalities,
            "adapter_input_modalities": input_modalities,
        }
    # 自定义模型注册即声明可用；实际连通性由连通测试与运行期错误反馈
    capability["api_contract_verified"] = True
    user_caps = item.get("capabilities")
    if isinstance(user_caps, dict):
        capability = _deep_merge_dict(capability, user_caps)
    if "video" in types:
        capability = _normalize_video_capabilities(capability)
    return capability


def _custom_model_entries() -> dict[str, dict[str, Any]]:
    """由有效配置（文件 + env）中的 custom_models 派生注册表条目。"""
    try:
        from config import CUSTOM_MODEL_TYPES, Config
    except Exception:  # pragma: no cover - 极端情况下注册表退化为仅内置模型
        logger.warning("Config unavailable while building custom model registry", exc_info=True)
        return {}
    config_values = Config.CONFIG if isinstance(Config.CONFIG, dict) else {}
    providers = config_values.get("api_providers", {}) or {}
    entries: dict[str, dict[str, Any]] = {}
    for item in config_values.get("custom_models", []) or []:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        provider_key = str(item.get("provider") or "").strip()
        if not model_id or not provider_key:
            continue
        # 供应商缺失/不完整时仍派生条目：由 model_availability 给出精确不可用原因
        provider_config = providers.get(provider_key)
        if not isinstance(provider_config, dict):
            provider_config = {}
        types = [t for t in (item.get("types") or []) if t in CUSTOM_MODEL_TYPES]
        if not types:
            continue
        try:
            concurrency = max(1, int(item.get("concurrency") or CUSTOM_MODEL_DEFAULT_CONCURRENCY))
        except (TypeError, ValueError):
            concurrency = CUSTOM_MODEL_DEFAULT_CONCURRENCY
        entries[model_id] = {
            # 模型列表统一以 id 展示（不再使用单独的显示名）
            "name": model_id,
            "provider": provider_key,
            "provider_label": provider_key,
            "family": "custom",
            "protocol": provider_config.get("protocol", ""),
            "base_url": provider_config.get("base_url", ""),
            "api_key": provider_config.get("api_key", ""),
            "request_model": item.get("model") or model_id,
            "type": list(types),
            "concurrency": concurrency,
            "custom": True,
            "capabilities": _custom_model_capabilities(item, types),
        }
    return entries


def merged_models() -> dict[str, dict[str, Any]]:
    """内置模型 + 自定义模型的合并视图（自定义条目以精确 id 优先）。"""
    merged = dict(_builtin_models())
    merged.update(_custom_model_entries())
    return merged


def resolve_model_entry(model: str) -> tuple[str, dict[str, Any]]:
    """解析模型归属：custom（走协议适配层）/ builtin（既有名称路由）/ unknown。"""
    if not model:
        return "unknown", {"name": model, "provider": "unknown", "type": []}
    custom_entries = _custom_model_entries()
    if model in custom_entries:
        return "custom", custom_entries[model]
    builtin = _builtin_models()
    if model in builtin:
        return "builtin", builtin[model]
    model_lower = model.lower()
    for key, value in builtin.items():
        # 保留既有别名兼容：内置模型名模糊匹配
        if key in model_lower or model_lower in key:
            return "builtin", value
    return "unknown", {"name": model, "provider": "unknown", "type": []}


def model_availability(model: str) -> dict[str, Any]:
    """阶段预检用：判断模型当前是否可用。

    返回 {"available": bool, "code": str, "reason": str}，code 取值：
    not_registered / provider_missing / provider_incomplete / missing_credentials / ""。
    """
    kind, metadata = resolve_model_entry(model)
    if kind == "unknown":
        return {
            "available": False,
            "code": "not_registered",
            "reason": f"模型 {model} 未注册（可在设置页注册为自定义模型或检查名称）",
        }
    try:
        from config import Config, CUSTOM_PROTOCOLS
    except Exception:  # pragma: no cover
        Config = None
        CUSTOM_PROTOCOLS = ()
    if kind == "custom":
        provider_key = str(metadata.get("provider") or "")
        config_values = Config.CONFIG if Config and isinstance(Config.CONFIG, dict) else {}
        provider_config = (config_values.get("api_providers", {}) or {}).get(provider_key)
        if not isinstance(provider_config, dict):
            return {
                "available": False,
                "code": "provider_missing",
                "reason": f"自定义模型 {model} 引用的供应商 {provider_key} 未定义",
            }
        base_url = str(provider_config.get("base_url") or "")
        if provider_config.get("protocol") not in CUSTOM_PROTOCOLS or not base_url.startswith(("http://", "https://")):
            return {
                "available": False,
                "code": "provider_incomplete",
                "reason": f"供应商 {provider_key} 缺少 protocol 或 base_url",
            }
        return {"available": True, "code": "", "reason": ""}

    provider = str(metadata.get("provider") or "")
    credential_field = PROVIDER_CREDENTIAL_FIELDS.get(provider)
    if credential_field:
        value = getattr(Config, credential_field, "") if Config else ""
        if not value:
            return {
                "available": False,
                "code": "missing_credentials",
                "reason": f"供应商 {provider} 的 API Key 未配置（{credential_field}）",
            }
    return {"available": True, "code": "", "reason": ""}


def load_model_config() -> dict[str, Any]:
    """返回合并后的模型注册表视图（内置 + 自定义）。"""
    return {"models": merged_models()}


def get_model_config(model: str) -> dict[str, Any]:
    """Get metadata for one model, with a loose fallback for provider aliases."""
    models = merged_models()
    if model in models:
        return models[model]

    model_lower = model.lower()
    for key, value in models.items():
        if key in model_lower or model_lower in key:
            return value

    return {
        "name": model,
        "provider": "unknown",
        "type": [],
        "concurrency": 3,
    }


def get_max_concurrency(model: str, enable_concurrency: bool = False) -> int:
    if not enable_concurrency:
        return 1
    return int(get_model_config(model).get("concurrency", 3))


def get_models_by_type(model_type: str) -> list[dict[str, Any]]:
    result = []
    for model_id, metadata in merged_models().items():
        if model_type in (metadata.get("type") or []):
            result.append({"id": model_id, **metadata})
    return result


def model_type_capabilities(model_type: str, metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Return normalized capability metadata for non-media model selectors."""
    metadata = metadata or {}
    if metadata.get("capabilities"):
        return deepcopy(metadata["capabilities"])
    if model_type == "llm":
        return {
            "ability_type": "text_generation",
            "ability_types": ["text_generation"],
            "adapter_ability_types": ["text_generation"],
            "input_modalities": ["text"],
            "adapter_input_modalities": ["text"],
            "api_contract_verified": bool(metadata.get("api_contract_verified", False)),
        }
    if model_type == "vlm":
        return {
            "ability_type": "vision_language",
            "ability_types": ["vision_language", "image_understanding"],
            "adapter_ability_types": ["vision_language", "image_understanding"],
            "input_modalities": ["text", "image"],
            "adapter_input_modalities": ["text", "image"],
            "api_contract_verified": bool(metadata.get("api_contract_verified", False)),
        }
    return media_capabilities(metadata.get("provider", ""), metadata.get("id", ""), "video" if model_type == "video" else "image")


def model_records(media_type: Optional[str] = None) -> list[dict[str, Any]]:
    records = []
    for model_id, metadata in merged_models().items():
        resolved_media_type = _resolve_media_type(metadata.get("type") or [])
        if media_type and resolved_media_type != media_type:
            continue
        if resolved_media_type not in {"image", "video"}:
            continue
        records.append(_workflow_info(model_id, metadata, resolved_media_type))
    return records


def list_api_models(
    media_type: Optional[str] = None,
    required_adapter_abilities: Optional[list[str]] = None,
    verified_only: bool = False,
) -> list[dict[str, Any]]:
    records = model_records(media_type=media_type)
    required = set(required_adapter_abilities or [])
    if verified_only:
        records = [record for record in records if record.get("api_contract_verified", True)]
    if required:
        records = [record for record in records if required.intersection(model_ability_tags(record))]
    return records


def parse_api_model(model: str, media_type: str) -> tuple[str, str]:
    if model and model.startswith("api/"):
        _, provider, model_id = model.split("/", 2)
        return provider, model_id

    metadata = merged_models().get(model)
    if metadata and _resolve_media_type(metadata.get("type") or []) == media_type:
        return metadata.get("provider", ""), model
    return "", model


def media_capabilities(provider: str, model: str, media_type: str) -> dict[str, Any]:
    metadata = merged_models().get(model, {})
    capabilities = metadata.get("capabilities")
    if capabilities:
        return deepcopy(capabilities)
    if media_type == "image":
        return {
            "ability_type": "image_generation",
            "ability_types": ["text_to_image"],
            "adapter_ability_types": ["text_to_image"],
            "input_modalities": ["text"],
            "adapter_input_modalities": ["text"],
            "api_contract_verified": False,
        }
    return {
        "ability_type": "image_to_video",
        "ability_types": [],
        "adapter_ability_types": ["first_frame_i2v"],
        "input_modalities": ["text", "image"],
        "adapter_input_modalities": ["text", "image"],
        "api_contract_verified": False,
        "duration": {"min": 3, "max": 15, "integer": True, "verified": False},
    }


def video_capabilities(provider: str, model: str) -> dict[str, Any]:
    return media_capabilities(provider, model, "video")


def image_capabilities(provider: str, model: str) -> dict[str, Any]:
    return media_capabilities(provider, model, "image")


def model_ability_tags(record: dict[str, Any]) -> set[str]:
    tags = set(record.get("adapter_ability_types") or [])
    tags.update(record.get("ability_types") or [])
    if record.get("ability_type"):
        tags.add(record["ability_type"])
    tags.update(record.get("type") or [])
    return tags


def _workflow_info(model_id: str, metadata: dict[str, Any], media_type: str) -> dict[str, Any]:
    provider = metadata.get("provider", "")
    capabilities = media_capabilities(provider, model_id, media_type)
    return {
        "key": f"api/{provider}/{model_id}",
        "name": model_id,
        "display_name": metadata.get("name") or model_id,
        "source": "api",
        "provider": provider,
        "provider_label": metadata.get("provider_label") or provider,
        "family": metadata.get("family"),
        "model": model_id,
        "media_type": media_type,
        "type": metadata.get("type", []),
        "capabilities": capabilities,
        "ability_type": capabilities.get("ability_type"),
        "ability_types": capabilities.get("ability_types", []),
        "adapter_ability_types": capabilities.get("adapter_ability_types", []),
        "input_modalities": capabilities.get("input_modalities", []),
        "adapter_input_modalities": capabilities.get("adapter_input_modalities", []),
        "api_contract_verified": capabilities.get("api_contract_verified", False),
        "concurrency": metadata.get("concurrency"),
    }


def _resolve_media_type(types: list[str]) -> str:
    if "video" in types:
        return "video"
    if any(item in types for item in ("t2i", "i2i", "image")):
        return "image"
    return ""
