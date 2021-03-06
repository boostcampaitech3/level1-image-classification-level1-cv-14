import os
import random
from collections import defaultdict
from enum import Enum
from typing import Tuple, List

import numpy as np
import torch
from PIL import Image
from torch.utils.data import Dataset, Subset, random_split
from torchvision import transforms
from torchvision.transforms import *

IMG_EXTENSIONS = [
    ".jpg", ".JPG", ".jpeg", ".JPEG", ".png",
    ".PNG", ".ppm", ".PPM", ".bmp", ".BMP",
]


def is_image_file(filename):
    return any(filename.endswith(extension) for extension in IMG_EXTENSIONS)

########################################## Augmentation ##########################################

class BaseAugmentation:
    def __init__(self, resize, mean, std, **args):
        self.transform = transforms.Compose([
            # Resize(resize, Image.BILINEAR),
            ToTensor(),
            Normalize(mean=mean, std=std),
        ])

    def __call__(self, image):
        return self.transform(image)


class AddGaussianNoise(object):
    """
        transform 에 없는 기능들은 이런식으로 __init__, __call__, __repr__ 부분을
        직접 구현하여 사용할 수 있습니다.
    """

    def __init__(self, mean=0., std=1.):
        self.std = std
        self.mean = mean

    def __call__(self, tensor):
        return tensor + torch.randn(tensor.size()) * self.std + self.mean

    def __repr__(self):
        return self.__class__.__name__ + '(mean={0}, std={1})'.format(self.mean, self.std)


class CustomAugmentation:
    def __init__(self, mean=(0.548, 0.504, 0.479), std=(0.237, 0.247, 0.246), gm=0, gs=1.0, **args):
        self.transform = transforms.Compose([
            # ToPILImage(),
            CenterCrop((384, 384)),
            # Resize((224, 224)),
            # ColorJitter(0.1, 0.1, 0.1, 0.1),
            ToTensor(),
            # Normalize(mean=mean, std=std),
            # AddGaussianNoise(mean=gm, std=gs)
        ])

class CropAugmentation:
    def __init__(self, mean=(0.548, 0.504, 0.479), std=(0.237, 0.247, 0.246), **args):
        self.transform = transforms.Compose([
            CenterCrop((384, 384)),
            ToTensor(),
            Normalize(mean=mean, std=std)
        ])

    def __call__(self, image):
        return self.transform(image)

class ViTAugmentation:
    def __init__(self, mean=(0.548, 0.504, 0.479), std=(0.237, 0.247, 0.246), **args):
        self.transform = transforms.Compose([
            CenterCrop((384, 384)),
            Resize((224, 224)),
            ToTensor(),
            Normalize(mean=mean, std=std),
        ])

    def __call__(self, image):
        return self.transform(image)

########################################## Labeling ##########################################

class MaskLabels(int, Enum):                                    # mask label
    MASK = 0
    INCORRECT = 1
    NORMAL = 2


class GenderLabels(int, Enum):                                  # gender label을 str -> int
    MALE = 0
    FEMALE = 1

    @classmethod
    def from_str(cls, value: str) -> int:
        value = value.lower()
        if value == "male":
            return cls.MALE
        elif value == "female":
            return cls.FEMALE
        else:
            raise ValueError(f"Gender value should be either 'male' or 'female', {value}")


class AgeLabels(int, Enum):                                     # age를 카테고리화
    YOUNG = 0
    MIDDLE = 1
    OLD = 2

    @classmethod
    def from_number(cls, value: str) -> int:
        try:
            value = int(value)
        except Exception:
            raise ValueError(f"Age value should be numeric, {value}")

        if value < 30:
            return cls.YOUNG
        elif value < 60:
            return cls.MIDDLE
        else:
            return cls.OLD

########################################## Dataset ##########################################

class MaskBaseDataset(Dataset):                                 ####################################### 기본 dataset
    # num_classes = 3 * 2 * 3

    _file_names = {
        "mask1": MaskLabels.MASK,
        "mask2": MaskLabels.MASK,
        "mask3": MaskLabels.MASK,
        "mask4": MaskLabels.MASK,
        "mask5": MaskLabels.MASK,
        "incorrect_mask": MaskLabels.INCORRECT,
        "normal": MaskLabels.NORMAL
    }

    image_paths = []
    mask_labels = []
    gender_labels = []
    age_labels = []

    # mean과 std는 RGB이므로 각각 3개
    def __init__(self, data_dir, mean=(0.548, 0.504, 0.479), std=(0.237, 0.247, 0.246), val_ratio=0.2):
        self.data_dir = data_dir
        self.mean = mean
        self.std = std
        self.val_ratio = val_ratio

        self.transform = None
        self.setup()
        self.calc_statistics()

    def setup(self):
        profiles = os.listdir(self.data_dir)
        for profile in profiles:                # 이미지 폴더들 (사람별)
            if profile.startswith("."):         # "." 로 시작하는 파일은 무시합니다
                continue

            img_folder = os.path.join(self.data_dir, profile)   
            for file_name in os.listdir(img_folder):            # 이미지들 (mask, normal 등)
                _file_name, ext = os.path.splitext(file_name)
                if _file_name not in self._file_names:          # "." 로 시작하는 파일 및 invalid 한 파일들은 무시합니다
                    continue

                img_path = os.path.join(self.data_dir, profile, file_name)  # (resized_data, 000004_male_Asian_54, mask1.jpg)
                mask_label = self._file_names[_file_name]                   # mask label = 파일 이름.

                id, gender, race, age = profile.split("_")
                # gender와 age label
                gender_label = GenderLabels.from_str(gender)
                age_label = AgeLabels.from_number(age)

                self.image_paths.append(img_path)
                self.mask_labels.append(mask_label)
                self.gender_labels.append(gender_label)
                self.age_labels.append(age_label)

    def calc_statistics(self):
        has_statistics = self.mean is not None and self.std is not None
        if not has_statistics:
            print("[Warning] Calculating statistics... It can take a long time depending on your CPU machine")
            sums = []
            squared = []
            for image_path in self.image_paths[:3000]:
                image = np.array(Image.open(image_path)).astype(np.int32)
                sums.append(image.mean(axis=(0, 1)))
                squared.append((image ** 2).mean(axis=(0, 1)))

            self.mean = np.mean(sums, axis=0) / 255
            self.std = (np.mean(squared, axis=0) - self.mean ** 2) ** 0.5 / 255

    def set_transform(self, transform):
        self.transform = transform

    def __getitem__(self, index):
        assert self.transform is not None, ".set_tranform 메소드를 이용하여 transform 을 주입해주세요"

        image = self.read_image(index)
        mask_label = self.get_mask_label(index)
        gender_label = self.get_gender_label(index)
        age_label = self.get_age_label(index)
        multi_class_label = self.encode_multi_class(mask_label, gender_label, age_label)

        image_transform = self.transform(image)
        return image_transform, multi_class_label

    def __len__(self):
        return len(self.image_paths)

    def get_mask_label(self, index) -> MaskLabels:
        return self.mask_labels[index]

    def get_gender_label(self, index) -> GenderLabels:
        return self.gender_labels[index]

    def get_age_label(self, index) -> AgeLabels:
        return self.age_labels[index]

    def read_image(self, index):
        image_path = self.image_paths[index]
        return Image.open(image_path)

    @staticmethod
    def encode_multi_class(mask_label, gender_label, age_label) -> int:
        return mask_label * 6 + gender_label * 3 + age_label

    @staticmethod
    def decode_multi_class(multi_class_label) -> Tuple[MaskLabels, GenderLabels, AgeLabels]:
        mask_label = (multi_class_label // 6) % 3
        gender_label = (multi_class_label // 3) % 2
        age_label = multi_class_label % 3
        return mask_label, gender_label, age_label

    @staticmethod
    def denormalize_image(image, mean, std):
        """
        normalization 된 이미지를 다시 복원.
        """
        img_cp = image.copy()
        img_cp *= std
        img_cp += mean
        img_cp *= 255.0
        img_cp = np.clip(img_cp, 0, 255).astype(np.uint8)
        return img_cp

    def split_dataset(self) -> Tuple[Subset, Subset]:
        """
        데이터셋을 train 과 val 로 나눕니다,
        pytorch 내부의 torch.utils.data.random_split 함수를 사용하여
        torch.utils.data.Subset 클래스 둘로 나눕니다.
        구현이 어렵지 않으니 구글링 혹은 IDE (e.g. pycharm) 의 navigation 기능을 통해 코드를 한 번 읽어보는 것을 추천드립니다^^
        """
        n_val = int(len(self) * self.val_ratio)                     # 전체에서 validation data의 비율.
        n_train = len(self) - n_val                                 
        train_set, val_set = random_split(self, [n_train, n_val])   # train data와 validation data로 나눔. (중복 없음)
        return train_set, val_set


class MaskSplitByProfileDataset(MaskBaseDataset):
    """
        train / val 나누는 기준을 이미지에 대해서 random 이 아닌
        사람(profile)을 기준으로 나눕니다.
        구현은 val_ratio 에 맞게 train / val 나누는 것을 이미지 전체가 아닌 사람(profile)에 대해서 진행하여 indexing 을 합니다
        이후 `split_dataset` 에서 index 에 맞게 Subset 으로 dataset 을 분기합니다.

        MaskBaseDataset은 사람에 상관 없이 전체 이미지를 비율로 나누어서 train와 val에 같은 사람이 들어갈 수 있었다.
        여기서는 개인을 기준으로 train과 val data를 비율대로 나눔.
        즉, 같은 사람이 동시에 들어가는 일이 없다. 
    """

    def __init__(self, data_dir, mean=(0.548, 0.504, 0.479), std=(0.237, 0.247, 0.246), val_ratio=0.2):
        self.indices = defaultdict(list)
        super().__init__(data_dir, mean, std, val_ratio)

    @staticmethod
    def _split_profile(profiles, val_ratio):
        length = len(profiles)
        n_val = int(length * val_ratio)

        val_indices = set(random.choices(range(length), k=n_val))
        train_indices = set(range(length)) - val_indices            # 차집합
        return {
            "train": train_indices,
            "val": val_indices
        }

    def setup(self):
        profiles = os.listdir(self.data_dir)
        profiles = [profile for profile in profiles if not profile.startswith(".")]
        split_profiles = self._split_profile(profiles, self.val_ratio)

        cnt = 0
        for phase, indices in split_profiles.items():               # phase: train, val
            for _idx in indices:
                profile = profiles[_idx]                            # 특정 사람의
                img_folder = os.path.join(self.data_dir, profile)
                for file_name in os.listdir(img_folder):            # 각각의 사진들
                    _file_name, ext = os.path.splitext(file_name)
                    if _file_name not in self._file_names:          # "." 로 시작하는 파일 및 invalid 한 파일들은 무시합니다
                        continue

                    img_path = os.path.join(self.data_dir, profile, file_name)  # (resized_data, 000004_male_Asian_54, mask1.jpg)
                    mask_label = self._file_names[_file_name]

                    id, gender, race, age = profile.split("_")
                    gender_label = GenderLabels.from_str(gender)
                    age_label = AgeLabels.from_number(age)

                    self.image_paths.append(img_path)
                    self.mask_labels.append(mask_label)
                    self.gender_labels.append(gender_label)
                    self.age_labels.append(age_label)

                    self.indices[phase].append(cnt)
                    cnt += 1

    def split_dataset(self) -> List[Subset]:
        return [Subset(self, indices) for phase, indices in self.indices.items()]

########################################## Custom Dataset ##########################################

class MaskLabelDataset(MaskBaseDataset):
    """
        원하는 mask label로 데이터 분류.
        label은 gender, age 뿐. 
    """
    num_classes = 2 * 3         # gender * age

    _mask_labels = {
        "mask": MaskLabels.MASK,
        "incorrect_mask": MaskLabels.INCORRECT,
        "normal": MaskLabels.NORMAL
    }

    def __init__(self, data_dir, mean=(0.548, 0.504, 0.479), std=(0.237, 0.247, 0.246), val_ratio=0.2,
                    mask_label='mask'):
        self.indices = defaultdict(list)
        self.mask_label = mask_label
        super().__init__(data_dir, mean, std, val_ratio)

    @staticmethod
    def _split_profile(profiles, val_ratio):
        length = len(profiles)
        n_val = int(length * val_ratio)

        val_indices = set(random.choices(range(length), k=n_val))
        train_indices = set(range(length)) - val_indices            # 차집합
        return {
            "train": train_indices,
            "val": val_indices
        }

    def setup(self):
        profiles = os.listdir(self.data_dir)
        profiles = [profile for profile in profiles if not profile.startswith(".")]
        split_profiles = self._split_profile(profiles, self.val_ratio)

        cnt = 0
        for phase, indices in split_profiles.items():               # phase: train, val
            for _idx in indices:
                profile = profiles[_idx]                            # 특정 사람의
                img_folder = os.path.join(self.data_dir, profile)
                for file_name in os.listdir(img_folder):            # 각각의 사진들
                    _file_name, ext = os.path.splitext(file_name)
                    if not _file_name.startswith(self.mask_label):  # 원하는 mask label의 데이터만
                        continue

                    img_path = os.path.join(self.data_dir, profile, file_name)  # (resized_data, 000004_male_Asian_54, mask1.jpg)
                    mask_label = self._mask_labels[self.mask_label]

                    id, gender, race, age = profile.split("_")
                    gender_label = GenderLabels.from_str(gender)
                    age_label = AgeLabels.from_number(age)

                    self.image_paths.append(img_path)
                    self.mask_labels.append(mask_label)
                    self.gender_labels.append(gender_label)
                    self.age_labels.append(age_label)

                    self.indices[phase].append(cnt)
                    cnt += 1

    def __getitem__(self, index):
        assert self.transform is not None, ".set_tranform 메소드를 이용하여 transform 을 주입해주세요"

        image = self.read_image(index)
        mask_label = self._mask_labels[self.mask_label]
        gender_label = self.get_gender_label(index)
        age_label = self.get_age_label(index)
        # multi_class_label = self.encode_multi_class(mask_label, gender_label, age_label)
        # cross entropy는 클래스 넘버가 무조건 0부터 시작해야함.
        # encoder를 그대로 사용하면 Assertion `t >= 0 && t < n_classes` failed 에러가 뜸.
        multi_class_label = gender_label * 3 + age_label

        image_transform = self.transform(image)
        return image_transform, multi_class_label

    def split_dataset(self) -> List[Subset]:
        temp = [Subset(self, indices) for phase, indices in self.indices.items()]
        # print(temp)
        return temp

class MaskDataset(MaskBaseDataset):
    """
        mask 착용 여부만 분류하도록 하는 dataset 
    """

    num_classes = 3     # mask, normal, incorrect_mask

    def __init__(self, data_dir, mean=(0.548, 0.504, 0.479), std=(0.237, 0.247, 0.246), val_ratio=0.2):
        self.indices = defaultdict(list)
        super().__init__(data_dir, mean, std, val_ratio)

    @staticmethod
    def _split_profile(profiles, val_ratio):
        length = len(profiles)
        n_val = int(length * val_ratio)

        val_indices = set(random.choices(range(length), k=n_val))
        train_indices = set(range(length)) - val_indices            # 차집합
        return {
            "train": train_indices,
            "val": val_indices
        }

    def setup(self):
        profiles = os.listdir(self.data_dir)
        profiles = [profile for profile in profiles if not profile.startswith(".")]
        split_profiles = self._split_profile(profiles, self.val_ratio)

        cnt = 0
        for phase, indices in split_profiles.items():               # phase: train, val
            for _idx in indices:
                profile = profiles[_idx]                            # 특정 사람의
                img_folder = os.path.join(self.data_dir, profile)
                for file_name in os.listdir(img_folder):            # 각각의 사진들
                    _file_name, ext = os.path.splitext(file_name)
                    if _file_name not in self._file_names:          # "." 로 시작하는 파일 및 invalid 한 파일들은 무시합니다
                        continue

                    img_path = os.path.join(self.data_dir, profile, file_name)  # (resized_data, 000004_male_Asian_54, mask1.jpg)
                    mask_label = self._file_names[_file_name]

                    self.image_paths.append(img_path)
                    self.mask_labels.append(mask_label)

                    self.indices[phase].append(cnt)
                    cnt += 1

    def split_dataset(self) -> List[Subset]:
        return [Subset(self, indices) for phase, indices in self.indices.items()]

    def __getitem__(self, index):
        assert self.transform is not None, ".set_tranform 메소드를 이용하여 transform 을 주입해주세요"

        image = self.read_image(index)
        mask_label = self.get_mask_label(index)

        image_transform = self.transform(image)
        return image_transform, mask_label

########################################## Test Dataset ##########################################

class TestDataset(Dataset):
    def __init__(self, img_paths, mean=(0.548, 0.504, 0.479), std=(0.237, 0.247, 0.246)):
        self.img_paths = img_paths
        self.transform = transforms.Compose([
            CenterCrop((384, 384)),
            ToTensor(),
            Normalize(mean=mean, std=std)
        ])

    def __getitem__(self, index):
        image = Image.open(self.img_paths[index])
        # print(self.img_paths[index])
        # print(ToTensor()(CenterCrop((384, 384))(image)).shape)

        if self.transform:
            image = self.transform(image)
        return image

    def __len__(self):
        return len(self.img_paths)


class EvalDataset(Dataset):
    _file_names = {
        "mask1": MaskLabels.MASK,
        "mask2": MaskLabels.MASK,
        "mask3": MaskLabels.MASK,
        "mask4": MaskLabels.MASK,
        "mask5": MaskLabels.MASK,
        "incorrect_mask": MaskLabels.INCORRECT,
        "normal": MaskLabels.NORMAL
    }
    
    image_paths = []
    mask_labels = []
    gender_labels = []
    age_labels = []

    def __init__(self, data_dir, ratio, mean=(0.548, 0.504, 0.479), std=(0.237, 0.247, 0.246)):
        self.data_dir = data_dir
        self.mean = mean
        self.std = std
        self.transform = None

        self.data_ratio = ratio

        self.setup()

    def setup(self):
        profiles = os.listdir(self.data_dir)
        for profile in profiles:                # 이미지 폴더들 (사람별)
            if profile.startswith("."):         # "." 로 시작하는 파일은 무시합니다
                continue

            img_folder = os.path.join(self.data_dir, profile)   
            for file_name in os.listdir(img_folder):            # 이미지들 (mask, normal 등)
                _file_name, ext = os.path.splitext(file_name)
                if _file_name not in self._file_names:          # "." 로 시작하는 파일 및 invalid 한 파일들은 무시합니다
                    continue

                is_in = random.choices([True, False], weights=[self.data_ratio, 1 - self.data_ratio])

                if not is_in:
                    continue

                img_path = os.path.join(self.data_dir, profile, file_name)  # (resized_data, 000004_male_Asian_54, mask1.jpg)
                mask_label = self._file_names[_file_name]                   # mask label = 파일 이름.

                id, gender, race, age = profile.split("_")
                # gender와 age label
                gender_label = GenderLabels.from_str(gender)
                age_label = AgeLabels.from_number(age)

                self.image_paths.append(img_path)
                self.mask_labels.append(mask_label)
                self.gender_labels.append(gender_label)
                self.age_labels.append(age_label)

    def __getitem__(self, index):
        image = Image.open(self.image_paths[index])
        mask_label = self.mask_labels[index]
        gender_label = self.gender_labels[index]
        age_label = self.age_labels[index]
        multi_class_label = mask_label * 6 + gender_label * 3 + age_label

        if self.transform:
            image = self.transform(image)
        return image, multi_class_label

    def __len__(self):
        return len(self.image_paths)

    def set_transform(self, transform):
        self.transform = transform


class ModelEvalDataset(Dataset):
    _file_names = {
        "mask1": MaskLabels.MASK,
        "mask2": MaskLabels.MASK,
        "mask3": MaskLabels.MASK,
        "mask4": MaskLabels.MASK,
        "mask5": MaskLabels.MASK,
        "incorrect_mask": MaskLabels.INCORRECT,
        "normal": MaskLabels.NORMAL
    }
    
    image_paths = []
    mask_labels = []
    gender_labels = []
    age_labels = []

    def __init__(self, data_dir, label, ratio, mean=(0.548, 0.504, 0.479), std=(0.237, 0.247, 0.246)):
        self.data_dir = data_dir
        self.mean = mean
        self.std = std
        self.transform = None
        self.label = label

        self.data_ratio = ratio

        self.setup()

    def setup(self):
        profiles = os.listdir(self.data_dir)
        for profile in profiles:                # 이미지 폴더들 (사람별)
            if profile.startswith("."):         # "." 로 시작하는 파일은 무시합니다
                continue

            img_folder = os.path.join(self.data_dir, profile)   
            for file_name in os.listdir(img_folder):            # 이미지들 (mask, normal 등)
                _file_name, ext = os.path.splitext(file_name)
                if not _file_name.startswith(self.label):          
                    continue

                is_in = random.choices([True, False], weights=[self.data_ratio, 1 - self.data_ratio])

                if not is_in:
                    continue

                img_path = os.path.join(self.data_dir, profile, file_name)  # (resized_data, 000004_male_Asian_54, mask1.jpg)

                id, gender, race, age = profile.split("_")
                # gender와 age label
                gender_label = GenderLabels.from_str(gender)
                age_label = AgeLabels.from_number(age)

                self.image_paths.append(img_path)
                self.gender_labels.append(gender_label)
                self.age_labels.append(age_label)

    def __getitem__(self, index):
        image = Image.open(self.image_paths[index])
        gender_label = self.gender_labels[index]
        age_label = self.age_labels[index]
        multi_class_label = gender_label * 3 + age_label

        if self.transform:
            image = self.transform(image)
        return image, multi_class_label

    def __len__(self):
        return len(self.image_paths)

    def set_transform(self, transform):
        self.transform = transform