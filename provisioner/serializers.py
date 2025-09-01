from rest_framework import serializers

class ProvisionSerializer(serializers.Serializer):
    secret1 = serializers.CharField()
    secret2 = serializers.CharField()
    client_ref = serializers.CharField(required=False, allow_blank=True)
    email = serializers.EmailField()
    company = serializers.CharField(required=False, allow_blank=True)
    subdomain = serializers.CharField(required=False, allow_blank=True)
