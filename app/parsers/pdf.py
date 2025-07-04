from .base import BaseParser
from loguru import logger
import pdfplumber
from PIL import Image
from app.vision import image_to_markdown, get_ocr_text, vision_client
import os
import base64
import asyncio
from app.config import config

class PdfParser(BaseParser):
    """PDF文件解析器"""
    
    @classmethod
    def get_supported_extensions(cls) -> list[str]:
        return ['.pdf']
    
    async def _process_image_concurrent(self, temp_img_path: str, img_name: str, page_num: int, img_idx: int):
        """并发处理单个图片的OCR和视觉识别"""
        try:
            # 并发执行OCR和视觉识别，添加异常保护
            try:
                ocr_task = get_ocr_text(temp_img_path)
                vision_task = self._get_vision_description(temp_img_path)
                
                ocr_text, vision_description = await asyncio.gather(ocr_task, vision_task)
            except Exception as ocr_vision_error:
                logger.warning(f"OCR/视觉识别失败，跳过处理 第{page_num}页 图片{img_idx + 1}: {ocr_vision_error}")
                ocr_text = "OCR处理失败"
                vision_description = "视觉识别失败"
            
            # 生成HTML标签格式
            alt_text = f"# OCR: {ocr_text} # Visual_Features: {vision_description}"
            html_img_tag = f'<img src="{img_name}" alt="{alt_text}" />'
            
            logger.info(f"成功处理PDF图片 第{page_num}页 图片{img_idx + 1}: {img_name}")
            return f"### 第 {page_num} 页 - 图片 {img_idx + 1}\n\n{html_img_tag}"
            
        except Exception as e:
            logger.warning(f"PDF图片处理失败，跳过该图片 第{page_num}页 图片{img_idx + 1}: {e}")
            return f"### 第 {page_num} 页 - 图片 {img_idx + 1}\n\n*图片处理失败，已跳过*"
    
    async def _get_vision_description(self, temp_img_path: str) -> str:
        """获取视觉模型描述"""
        if not vision_client:
            return "视觉模型未配置"
        
        try:
            # 读取图片并转换为base64
            with open(temp_img_path, "rb") as image_file:
                base64_image = base64.b64encode(image_file.read()).decode('utf-8')
            
            response = vision_client.chat.completions.create(
                model=os.getenv("VISION_MODEL", "gpt-4o-mini"),
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{base64_image}"
                                }
                            },
                            {
                                "type": "text", 
                                "text": "Please provide a detailed description of this image, including: 1. Overall accurate description of the image; 2. Main elements and structure of the image; 3. If there are tables, charts, etc., please describe their content and layout in detail."
                            }
                        ]
                    }
                ],
                max_tokens=1000
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.warning(f"Vision API调用失败: {e}")
            return "视觉模型识别失败"

    async def parse(self, file_path: str) -> str:
        """解析PDF文件"""
        try:
            content_parts = []
            total_image_count = 0
            
            with pdfplumber.open(file_path) as pdf:
                # 首先统计总图片数量
                for page in pdf.pages:
                    total_image_count += len(page.images)
                
                # 图片数量保护机制
                max_imgs = config.MAX_IMAGES_PER_DOC
                if max_imgs != -1 and total_image_count > max_imgs:
                    logger.warning(f"PDF文档包含 {total_image_count} 张图片，超过{max_imgs}张限制，跳过所有图片处理")
                    skip_images = True
                else:
                    skip_images = False
                
                for page_num, page in enumerate(pdf.pages, 1):
                    # 提取文本
                    text = page.extract_text()
                    if text and text.strip():
                        content_parts.append(f"## 第 {page_num} 页\n\n{text.strip()}")
                    
                    # 处理图片（如果未跳过）
                    if not skip_images:
                        images = page.images
                        if images:
                            image_tasks = []
                            
                            for img_idx, img in enumerate(images):
                                try:
                                    # 提取图片
                                    bbox = (img['x0'], img['top'], img['x1'], img['bottom'])
                                    cropped_page = page.crop(bbox)
                                    
                                    # 保存为临时图片文件
                                    temp_img_path = self.create_temp_file(suffix='.png')
                                    
                                    # 将页面转换为图片
                                    page_img = cropped_page.to_image(resolution=150)
                                    page_img.save(temp_img_path, format='PNG')
                                    
                                    # 生成图片文件名
                                    base_name = os.path.splitext(os.path.basename(file_path))[0]
                                    img_name = f"{base_name}_page{page_num}_image_{img_idx + 1}.png"
                                    
                                    # 添加到并发任务列表
                                    task = self._process_image_concurrent(temp_img_path, img_name, page_num, img_idx)
                                    image_tasks.append(task)
                                    
                                except Exception as img_error:
                                    logger.warning(f"PDF图片提取失败，跳过该图片 第{page_num}页 图片{img_idx + 1}: {img_error}")
                                    content_parts.append(f"### 第 {page_num} 页 - 图片 {img_idx + 1}\n\n*图片提取失败，已跳过*")
                            
                            # 并发处理当前页面的所有图片
                            if image_tasks:
                                try:
                                    image_results = await asyncio.gather(*image_tasks, return_exceptions=True)
                                    for result in image_results:
                                        if isinstance(result, Exception):
                                            logger.error(f"图片并发处理异常，跳过该图片: {result}")
                                            content_parts.append("### 图片处理异常\n\n*图片处理异常，已跳过*")
                                        else:
                                            content_parts.append(result)
                                except Exception as e:
                                    logger.error(f"图片并发处理失败，跳过所有图片: {e}")
                                    content_parts.append("### 图片处理失败\n\n*图片并发处理失败，已跳过所有图片*")
                    else:
                        # 跳过图片处理时的提示
                        images = page.images
                        if images:
                            content_parts.append(f"### 第 {page_num} 页包含 {len(images)} 张图片\n\n*因图片总数超过{max_imgs}张限制，已跳过图片处理*")
            
            raw_content = '\n\n'.join(content_parts)
            
            # 处理换行符
            raw_content = raw_content.replace('\\n', '\n')
            
            # 将代码块转换为HTML标签
            raw_content = self.convert_code_blocks_to_html(raw_content)
            
            # 格式化为统一的代码块格式
            markdown_content = f"```document\n{raw_content or 'PDF文件为空或无法提取内容'}\n```"
            
            logger.info(f"成功解析PDF文件: {file_path} (总图片数: {total_image_count}, 跳过图片: {skip_images})")
            return markdown_content
            
        except Exception as e:
            logger.error(f"解析PDF文件失败 {file_path}: {e}")
            raise Exception(f"PDF文件解析错误: {str(e)}") 