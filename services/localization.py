CATALOGS = {
    "en": {
        "login": "Sign in",
        "email": "Email address",
        "password": "Password",
        "logout": "Sign out",
    },
    "hi": {
        "login": "साइन इन",
        "email": "ईमेल पता",
        "password": "पासवर्ड",
        "logout": "साइन आउट",
    },
}


def get_catalog(language: str) -> dict:
    return CATALOGS.get(language, CATALOGS["en"])
