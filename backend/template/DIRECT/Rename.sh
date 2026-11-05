for file in Dell_s5248F*; do
    new_name=$(echo "$file" | sed 's/_/ /g')
    mv "$file" "$new_name"
done
