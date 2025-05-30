import logging
import tempfile
import requests
import pdfplumber
import os
import io
import base64
from pdf2image import convert_from_path
import pytesseract
import gc
from google import genai as genai_embedding
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)

class EBookHandler:
    def __init__(self, openai_client, supabase_client, genai):
        self.openai_client = openai_client
        self.supabase = supabase_client
        self.genai = genai
        self.genai_embedding_client = genai_embedding.Client(api_key="GEMINI_API_KEY")

    def generate_embedding(self, text, use_gemini=False):
        """Generates embeddings for the given text using OpenAI's API or Gemini."""
        try:
            if use_gemini:
                result = self.genai_embedding_client.models.embed_content(
                        model="gemini-embedding-exp-03-07",
                        contents=text)
                return result.embeddings
            
            else:
                response = self.openai_client.embeddings.create(
                    model="text-embedding-3-small",
                    input=text,
                    encoding_format="float"
                )
                return response.data[0].embedding
        except Exception as e:
            logger.error(f"Failed to generate embedding: {str(e)}")
            return None

    def write_to_supabase(self, book_id, page_data):
        """Writes a single page/chapter to Supabase immediately after processing it."""
        try:
            logger.info(f"Writing page/chapter {page_data['page_number']} to Supabase for book_id: {book_id}")
            self.supabase.table('book_pages').insert(page_data).execute()
        except Exception as e:
            logger.error(f"Failed to write to Supabase: {str(e)}")

    # ===== PDF Processing Methods =====
    
    def download_file(self, file_url, file_type):
        """Downloads a file from the given URL using a streaming approach to avoid memory overload."""
        logger.info(f"Downloading {file_type.upper()} from URL: {file_url}")
        
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': f'application/{file_type}'
        }
        
        suffix = f'.{file_type}'
        
        with requests.get(file_url, stream=True, headers=headers) as response:
            response.raise_for_status()
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
                temp_filename = temp_file.name
                for chunk in response.iter_content(chunk_size=8192):
                    temp_file.write(chunk)
        
        logger.info(f"Successfully downloaded {file_type.upper()} to {temp_filename}")
        return temp_filename

    def process_pdf_page(self, pdf_path, page_num):
        """Process a single PDF page, trying text extraction first, falling back to OCR if needed."""
        logger.info(f"Processing PDF page {page_num}")
        
        text = ""
        
        # Try text extraction first
        try:
            with pdfplumber.open(pdf_path) as pdf:
                page = pdf.pages[page_num]
                text = page.extract_text() or ""
                
                # Check if we got meaningful text
                if text and len(text.strip()) > 50:
                    logger.info(f"Successfully extracted text from page {page_num}")
                    return text
        except Exception as e:
            logger.error(f"Error during text extraction for page {page_num}: {str(e)}")
            # Continue to OCR if text extraction fails
        
        # Only try OCR if text extraction didn't yield good results
        try:
            logger.info(f"Text extraction insufficient for page {page_num}, trying OCR")
            images = convert_from_path(pdf_path, dpi=100, first_page=page_num + 1, last_page=page_num + 1)
            
            if images:
                try:
                    ocr_text = pytesseract.image_to_string(images[0], config="--psm 6")
                    if ocr_text and len(ocr_text.strip()) > 0:
                        text = ocr_text
                except Exception as e:
                    logger.error(f"OCR processing failed for page {page_num}: {str(e)}")
                finally:
                    # Clean up images regardless of OCR success/failure
                    del images[0]
                    gc.collect()
        except Exception as e:
            logger.error(f"Error during PDF to image conversion for page {page_num}: {str(e)}")
        
        return text.strip()
    
    def convert_page_to_image(self, pdf_path, page_num):
        """Converts a specific page of a PDF to a PIL Image object."""
        try:
            images = convert_from_path(pdf_path, dpi=200, first_page=page_num + 1, last_page=page_num + 1)
            if images:
                return images[0]
            return None
        except Exception as e:
            logger.error(f"Error converting page {page_num + 1} to image: {str(e)}")
            return None

    def image_to_text_gemini(self, image):
        """Uses Google Gemini to extract text from an image."""
        try:
            if image:
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                image_bytes = buffer.getvalue()
                base64_image = base64.b64encode(image_bytes).decode('utf-8')
                mime_type = "image/png"  # Assuming you save as PNG

                image_part = {
                    "inline_data": {
                        "data": base64_image,
                        "mime_type": mime_type
                    }
                }

                model = self.genai.GenerativeModel('gemini-2.0-flash')

                # Prepare the prompt
                prompt = """
                    This is a page from a book. Extract all the main text from this image.
                """
                # Generate content with the image
                response = model.generate_content([prompt, image_part])
                response.resolve()  # Ensure the response is fully resolved
                return response.text if response.text else None
            
            return None
        except Exception as e:
            logger.error(f"Error during Gemini image to text conversion: {str(e)}")
            return None
        
    def process_pdf_page_with_gemini(self, pdf_path, page_num):
        """Processes a single PDF page by converting it to an image and using Gemini to extract text."""
        logger.info(f"Processing PDF page {page_num + 1} with Gemini")
        text = None
        image = self.convert_page_to_image(pdf_path, page_num)
        if image:
            text = self.image_to_text_gemini(image)
            gc.collect()  # Clean up image from memory
            if text:
                return text.strip()
            else:
                logger.warning(f"Gemini OCR failed to extract text from page {page_num + 1}")
                return ""
        else:
            logger.error(f"Failed to convert page {page_num + 1} to image for Gemini OCR.")
            return ""

    def process_pdf(self, pdf_url, book_id, page_count, use_gemini=False):
        """Process a PDF file, handling both text-based and OCR-based PDFs."""
        try:
            temp_filename = self.download_file(pdf_url, 'pdf')
            logger.info("Starting page-by-page processing for PDF")

            with pdfplumber.open(temp_filename) as pdf:
                total_pages = min(len(pdf.pages), page_count)

                for page_num in range(total_pages):
                    logger.info(f"Processing page {page_num + 1}/{total_pages}")
                    
                    if use_gemini:
                        text = self.process_pdf_page_with_gemini(temp_filename, page_num)
                    else:
                        text = self.process_pdf_page(temp_filename, page_num)
                    
                    if text.strip():  # Only process non-empty pages
                        embedding = self.generate_embedding(text)
                        page_data = {
                            "book_id": book_id,
                            "page_number": page_num + 1,
                            "text": text,
                            "embedding": embedding,
                        }
                        
                        self.write_to_supabase(book_id, page_data)
                    
                    gc.collect()  # Help manage memory

            os.unlink(temp_filename)
            logger.info(f"Successfully processed PDF with {total_pages} pages")

            return {
                'success': True,
                'message': 'PDF processed and stored successfully',
                'pageCount': total_pages
            }
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to download PDF: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Error processing PDF: {str(e)}")
            raise



  
    def process_epub_from_supabase(self, book_id, use_gemini=False):
        """
        Process an EPUB by generating embeddings for existing text in Supabase.
        This method reads the page text directly from Supabase and updates with embeddings.
        """
        try:
            logger.info(f"Starting embedding generation for book_id: {book_id} from Supabase data")
            
            # Fetch all pages for this book from Supabase
            response = self.supabase.table('book_pages').select('id, page_number, text').eq('book_id', book_id).execute()
            
            if not response.data:
                logger.warning(f"No pages found in Supabase for book_id: {book_id}")
                return {
                    'success': False,
                    'message': 'No pages found in Supabase for this book',
                    'pageCount': 0
                }
                
            total_pages = len(response.data)
            processed_pages = 0
            
            for page in response.data:
                page_id = page['id']
                page_number = page['page_number']
                text = page['text']
                
                logger.info(f"Generating embedding for page {page_number}/{total_pages}")
                
                if text and text.strip():
                    embedding = self.generate_embedding(text, use_gemini)
                    
                    # Update the existing page with the embedding
                    self.supabase.table('book_pages').update({
                        "embedding": embedding
                    }).eq('id', page_id).execute()
                    
                    processed_pages += 1
                    
                gc.collect()  # Help manage memory
                
            logger.info(f"Successfully generated embeddings for {processed_pages} pages")
            
            return {
                'success': True,
                'message': 'Embeddings generated and stored successfully',
                'pageCount': processed_pages
            }
        except Exception as e:
            logger.error(f"Error generating embeddings: {str(e)}")
            raise
