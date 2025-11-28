import os
import boto3
from botocore.exceptions import ClientError
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# S3 bucket name
AWS_STORAGE_BUCKET_NAME = os.environ.get('AWS_STORAGE_BUCKET_NAME')

# S3 access credentials
AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY')

# S3 endpoint for TimeWeb Cloud
AWS_S3_ENDPOINT_URL = os.environ.get('AWS_S3_ENDPOINT_URL', 'https://s3.timeweb.cloud')

# S3 region
AWS_S3_REGION_NAME = os.environ.get('AWS_S3_REGION_NAME', 'ru-1')

class S3Storage:
    def __init__(self):
        """Инициализация клиента S3"""
        self.s3_client = boto3.client(
            's3',
            endpoint_url=AWS_S3_ENDPOINT_URL,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_S3_REGION_NAME,
            use_ssl=False,
        )
        self.bucket_name = AWS_STORAGE_BUCKET_NAME
        # Базовая папка для "старого" бота (оставляем как есть, чтобы не ломать существующую логику)
        self.base_folder = '__tg_bot_photos'

    def _normalize_path(self, path):
        """Нормализация пути"""
        # Убираем лишние слеши и нормализуем
        normalized = os.path.normpath(path).replace('\\', '/')
        # Убираем начальный слеш если есть
        if normalized.startswith('/'):
            normalized = normalized[1:]
        return normalized

    def create_folder(self, folder_path):
        """Создание новой папки (директории) в S3"""
        normalized_path = self._normalize_path(folder_path)
        print(f"Creating folder: '{normalized_path}'")

        # Добавляем '/' в конец для S3
        s3_folder_key = normalized_path
        if not s3_folder_key.endswith('/'):
            s3_folder_key += '/'

        try:
            # Проверяем, существует ли уже объект с таким ключом
            try:
                self.s3_client.head_object(Bucket=self.bucket_name, Key=s3_folder_key)
                # Если head_object успешен, папка уже существует
                print(f"Folder '{normalized_path}' already exists")
                return True
            except ClientError as e:
                # Если получаем 404, значит объекта нет - это то, что нам нужно
                if e.response['Error']['Code'] == '404':
                    pass
                else:
                    # Если другая ошибка при проверке - пробрасываем ее
                    raise

            # Создаем пустой объект с '/' в конце имени
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=s3_folder_key,
                Body=''
            )
            print(f"Successfully created folder: '{normalized_path}'")
            return True

        except ClientError as e:
            print(f"Error creating folder '{normalized_path}': {str(e)}")
            return False

    def _timestamp_folder(self):
        """Имя папки как таймстемп YYYYMMDD_HHMMSS"""
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def create_department_path(self, department, folder_timestamp: str):
        """Создание пути для отдела и заданной таймстемп-папки в S3"""
        path_parts = [self.base_folder, department, folder_timestamp]
        folder_path = '/'.join(path_parts)
        return self.create_folder(folder_path)

    def save_photo(self, photo_data, user_id, department, folder_timestamp: str):
        """Сохранение фото в S3: __tg_bot_photos/department/<timestamp>/filename.jpg"""
        try:
            # Создаем путь для отдела
            self.create_department_path(department, folder_timestamp)
            
            # Формируем имя файла: user_id + текущая дата
            current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{user_id}_{current_time}.jpg"
            
            # Формируем полный путь в S3: __tg_bot_photos/department/<timestamp>/filename
            path_parts = [self.base_folder, department, folder_timestamp, filename]
            
            s3_key = '/'.join(path_parts)
            
            # Сохраняем фото в S3
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=photo_data,
                ContentType='image/jpeg'
            )
            
            print(f"Photo saved to S3: {s3_key}")
            return s3_key
            
        except Exception as e:
            print(f"Error saving photo to S3: {str(e)}")
            return None

    def save_video(self, video_data, user_id, department, folder_timestamp: str):
        """Сохранение видео в S3: __tg_bot_photos/department/<timestamp>/filename.mp4"""
        try:
            # Создаем путь для отдела
            self.create_department_path(department, folder_timestamp)

            # Формируем имя файла: user_id + текущая дата
            current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{user_id}_{current_time}.mp4"

            # Формируем полный путь в S3: __tg_bot_photos/department/<timestamp>/filename
            path_parts = [self.base_folder, department, folder_timestamp, filename]

            s3_key = '/'.join(path_parts)

            # Сохраняем видео в S3
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=video_data,
                ContentType='video/mp4'
            )

            print(f"Video saved to S3: {s3_key}")
            return s3_key

        except Exception as e:
            print(f"Error saving video to S3: {str(e)}")
            return None

    def save_text(self, text_data: str, user_id: int, department: str, folder_timestamp: str):
        """Сохранение текстового описания в S3 в txt-файл"""
        try:
            self.create_department_path(department, folder_timestamp)
            current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{user_id}_{current_time}.txt"
            path_parts = [self.base_folder, department, folder_timestamp, filename]
            s3_key = '/'.join(path_parts)

            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=text_data.encode('utf-8'),
                ContentType='text/plain; charset=utf-8'
            )
            print(f"Text saved to S3: {s3_key}")
            return s3_key
        except Exception as e:
            print(f"Error saving text to S3: {str(e)}")
            return None

    def ensure_base_folder_exists(self):
        """Проверка и создание базовой папки __tg_bot_photos"""
        return self.create_folder(self.base_folder)

    # ===== Методы для работы с дефектами в папке __tg_bot_photos_defect =====

    @property
    def defect_base_folder(self) -> str:
        """
        Базовая папка для хранения данных по дефектам.

        Структура:
        __tg_bot_photos_defect/<defect_id>/data_<defect_id>.json
        __tg_bot_photos_defect/<defect_id>/photo_1.jpg
        __tg_bot_photos_defect/<defect_id>/video_1.mp4
        и т.д.
        """

        return "__tg_bot_photos_defect"

    def get_defect_folder(self, defect_id: str) -> str:
        """Получить путь к папке конкретного дефекта."""

        return f"{self.defect_base_folder}/{defect_id}"

    def ensure_defect_base_folder_exists(self) -> bool:
        """Проверить и создать базовую папку __tg_bot_photos_defect, если её нет."""

        return self.create_folder(self.defect_base_folder)

    def create_defect_folder(self, defect_id: str) -> bool:
        """Создать папку для конкретного дефекта."""

        return self.create_folder(self.get_defect_folder(defect_id))

    def save_defect_json(self, defect_id: str, json_data: str) -> bool:
        """
        Сохранить JSON‑файл с данными дефекта.

        Имя файла: data_<id>.json
        """

        folder = self.get_defect_folder(defect_id)
        key = f"{folder}/data_{defect_id}.json"

        try:
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=json_data.encode("utf-8"),
                ContentType="application/json; charset=utf-8",
            )
            print(f"Defect JSON saved to S3: {key}")
            return True
        except Exception as e:
            print(f"Error saving defect JSON to S3 ({key}): {str(e)}")
            return False

    def load_defect_json(self, defect_id: str) -> str | None:
        """
        Загрузить JSON‑файл дефекта как строку.

        Возвращает None, если файл не найден или произошла ошибка.
        """

        folder = self.get_defect_folder(defect_id)
        key = f"{folder}/data_{defect_id}.json"

        try:
            obj = self.s3_client.get_object(Bucket=self.bucket_name, Key=key)
            return obj["Body"].read().decode("utf-8")
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                print(f"Defect JSON not found in S3: {key}")
            else:
                print(f"Error loading defect JSON from S3 ({key}): {str(e)}")
            return None

    def list_defect_objects(self, defect_id: str) -> list[dict]:
        """
        Получить список всех объектов (файлов) внутри папки дефекта.

        Удобно для последующего удаления/анализа.
        """

        prefix = self.get_defect_folder(defect_id)
        # Гарантируем завершающий слэш для корректного фильтра по префиксу
        if not prefix.endswith("/"):
            prefix += "/"

        try:
            response = self.s3_client.list_objects_v2(
                Bucket=self.bucket_name,
                Prefix=prefix,
            )
            return response.get("Contents", []) or []
        except Exception as e:
            print(f"Error listing defect objects in S3 (prefix={prefix}): {str(e)}")
            return []

    def delete_defect_files_by_prefix(self, defect_id: str, filename_prefix: str) -> None:
        """
        Удалить файлы внутри папки дефекта по префиксу имени.

        Например:
        filename_prefix='photo_' удалит все photo_*.*
        filename_prefix='video_' удалит все video_*.*
        """

        objects = self.list_defect_objects(defect_id)
        to_delete = []

        for obj in objects:
            key: str = obj.get("Key", "")
            # Берём только имя файла после последнего слеша
            filename = key.rsplit("/", 1)[-1]
            if filename.startswith(filename_prefix):
                to_delete.append({"Key": key})

        if not to_delete:
            return

        try:
            self.s3_client.delete_objects(
                Bucket=self.bucket_name,
                Delete={"Objects": to_delete},
            )
            print(f"Deleted {len(to_delete)} objects for defect_id={defect_id}, prefix={filename_prefix}")
        except Exception as e:
            print(f"Error deleting defect objects (defect_id={defect_id}, prefix={filename_prefix}): {str(e)}")

    def save_defect_file(self, defect_id: str, filename: str, data: bytes, content_type: str) -> str | None:
        """
        Сохранить произвольный файл (фото/видео и т.п.) в папку дефекта.

        Возвращает полный ключ (path) в S3 или None при ошибке.
        """

        folder = self.get_defect_folder(defect_id)
        key = f"{folder}/{filename}"

        try:
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
            print(f"Defect file saved to S3: {key}")
            return key
        except Exception as e:
            print(f"Error saving defect file to S3 ({key}): {str(e)}")
            return None

    def get_last_defect_number(self) -> int:
        """
        Получить последний использованный номер дефекта из файла last_id.txt в S3.

        Если файла нет, возвращает 0 (следующий будет D1).
        """

        key = f"{self.defect_base_folder}/last_id.txt"

        try:
            obj = self.s3_client.get_object(Bucket=self.bucket_name, Key=key)
            content = obj["Body"].read().decode("utf-8").strip()
            return int(content)
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                print(f"last_id.txt not found, starting from D1")
                return 0
            else:
                print(f"Error reading last_id.txt: {str(e)}")
                return 0
        except (ValueError, AttributeError) as e:
            print(f"Error parsing last_id.txt: {str(e)}")
            return 0

    def save_last_defect_number(self, number: int) -> bool:
        """
        Сохранить последний использованный номер дефекта в файл last_id.txt в S3.
        """

        key = f"{self.defect_base_folder}/last_id.txt"

        try:
            # Создаём базовую папку, если её нет
            self.ensure_defect_base_folder_exists()

            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=str(number).encode("utf-8"),
                ContentType="text/plain; charset=utf-8",
            )
            print(f"Last defect number saved: {number}")
            return True
        except Exception as e:
            print(f"Error saving last_id.txt: {str(e)}")
            return False

    def file_exists(self, defect_id: str, filename: str) -> bool:
        """
        Проверить существование файла в папке дефекта.
        
        Args:
            defect_id: ID дефекта
            filename: Имя файла
        
        Returns:
            True если файл существует, False в противном случае
        """
        folder = self.get_defect_folder(defect_id)
        key = f"{folder}/{filename}"
        
        try:
            self.s3_client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                return False
            print(f"Error checking file existence ({key}): {str(e)}")
            return False

    def get_file_url(self, defect_id: str, filename: str, expires_in: int = 3600) -> str | None:
        """
        Получить публичную URL для файла дефекта из S3.
        
        Args:
            defect_id: ID дефекта
            filename: Имя файла (например, photo_1.jpg, video_1.mp4)
            expires_in: Время жизни ссылки в секундах (по умолчанию 1 час)
        
        Returns:
            Публичная URL или None при ошибке
        """
        
        folder = self.get_defect_folder(defect_id)
        key = f"{folder}/{filename}"
        
        try:
            # Генерируем presigned URL
            url = self.s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': self.bucket_name, 'Key': key},
                ExpiresIn=expires_in
            )
            return url
        except Exception as e:
            print(f"Error generating URL for {key}: {str(e)}")
            return None

# Создаем глобальный экземпляр для использования в боте
s3_storage = S3Storage()